# References:
    # https://github.com/giovanniguidi/deeplabV3-PyTorch/tree/master/models

import torch
import torch.nn as nn
import torch.nn.functional as F
import ssl
from torchvision.models import resnet50, ResNet50_Weights
from torchvision.models.segmentation import deeplabv3_resnet50

ssl._create_default_https_context = ssl._create_unverified_context

RESNET50 = resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)


class ResNet50Backbone(nn.Module):
    def __init__(self):
        super().__init__()

        self.conv1_pool1 = nn.Sequential(
            RESNET50.conv1, RESNET50.bn1, RESNET50.relu, RESNET50.maxpool,RESNET50.layer1,
        )
        self.block1 = RESNET50.layer2
        self.block2 = RESNET50.layer3
        self.block3 = RESNET50.layer4

    def forward(self, x):
        x = self.conv1_pool1(x) # `output_stride = 4`
        x = self.block1(x) # `output_stride = 8`
        x = self.block2(x) # `output_stride = 16`
        x = self.block3(x) # `output_stride = 32`
        return x


class ConvBlock(nn.Module):
    def __init__(self, in_channels, kernel_size, dilation):
        super().__init__()

        self.in_channels = in_channels
        self.kernel_size = kernel_size
        self.dilation = dilation

        # "All with 256 filters and batch normalization"
        self.conv = nn.Conv2d(
            in_channels,
            256,
            kernel_size=kernel_size,
            dilation=dilation,
            padding="same",
            bias=False
        )
        self.bn = nn.BatchNorm2d(256)
        self.relu = nn.ReLU()
    
    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x


class ImagePooling(nn.Module):
    def __init__(self):
        super().__init__()

        self.global_avg_pool = nn.AdaptiveAvgPool2d(output_size=1)
        self.conv = nn.Conv2d(64, 256, kernel_size=1, bias=False)
        self.bn = nn.BatchNorm2d(256)
        self.relu = nn.ReLU()
    
    def forward(self, x): # `(b, 64, h, w)`
        _, _, w, h = x.shape

        # "We apply global average pooling on the last feature map of the model, feed
        # the resulting image-level features to a 1×1 convolution with $256$ filters
        # (and batch normalization), and then bilinearly upsample the feature to the desired spatial dimension.
        x = self.global_avg_pool(x) # `(b, 64, 1, 1)`
        x = self.conv(x) # `(b, 256, 1, 1)`
        x = self.bn(x)
        x = self.relu(x)
        x = F.interpolate(x, size=(w, h), mode="bilinear", align_corners=False) # `(b, 256, h, w)`
        return x


class ASPP(nn.Module):
    def __init__(self, atrous_rates):
        super().__init__()

        self.atrous_rates = atrous_rates

        # "ASPP consists of (a) one 1×1 convolution and three 3×3 convolutions
        # with `rates = (6, 12, 18)` when `output_stride = 16`, and (b) the image-level features.
        # "Four parallel atrous convolutions with different atrous rates are applied on top of the feature map."
        # "We include batch normalization within ASPP."
        self.conv_block1 = ConvBlock(in_channels=64, kernel_size=1, dilation=1)
        self.conv_block2 = ConvBlock(in_channels=64, kernel_size=3, dilation=atrous_rates[0])
        self.conv_block3 = ConvBlock(in_channels=64, kernel_size=3, dilation=atrous_rates[1])
        self.conv_block4 = ConvBlock(in_channels=64, kernel_size=3, dilation=atrous_rates[2])
        self.image_pooling = ImagePooling()
    
    def forward(self, x): # `(b, 64, h, w)`
        x1 = self.conv_block1(x) # `(b, 256, h, w)`
        x2 = self.conv_block2(x) # `(b, 256, h, w)`
        x3 = self.conv_block3(x) # `(b, 256, h, w)`
        x4 = self.conv_block4(x) # `(b, 256, h, w)`
        x5 = self.image_pooling(x) # `(b, 256, h, w)`
        # The resulting features from all the branches are then concatenated."
        x = torch.cat([x1, x2, x3, x4, x5], dim=1) # `(b, 256 * 5, h, w)`
        return x


# "Our best model is the case where block7 and (r1; r2; r3) = (1; 2; 1) are employed. Inference strategy on val set: The proposed"
class DeepLabv3(nn.Module):
    def __init__(self, output_stride=16, n_classes=21):
        super().__init__()

        self.n_classes = n_classes

        # "we apply atrous convolution with rates determined by the desired output stride value."
        # Note that the rates are doubled when `output_stride = 8`.
        if output_stride == 16:
            self.atrous_rates = (6, 12, 18)

        # "There are three 3×3 convolutions in those blocks."
        # "The last convolution contains stride $2$ except the one in last block."
        self.backbone = ResNet50Backbone()
        self.block4 = deeplabv3_resnet50().backbone.layer4
        
        self.aspp = ASPP(atrous_rates=self.atrous_rates)
        # "Pass through another 1×1 convolution (also with 256 filters and batch normalization)
        # before the final 1×1 convolution which generates the final logits."
        self.conv_block = ConvBlock(in_channels=1280, kernel_size=1, dilation=1)
        self.fin_conv = nn.Conv2d(256, n_classes, kernel_size=1)
    
    def forward(self, x):
        x = self.backbone(x)
        x = self.block4(x)
        x = self.aspp(x)
        x = self.conv_block(x)
        x = self.fin_conv(x)
        return x
deeplabv3 = DeepLabv3()
x = torch.randn(2, 3, 224, 224)
out = deeplabv3(x)

# "we adopt different atrous rates within block4 to block7 in the proposed model. In particular, we define as Multi Grid = (r1; r2; r3) the unit rates for the three convolutional layers within block4 to block7. The final atrous rate for the convolutional layer is equal to the multiplication of the unit rate and the corresponding rate. For example, when output stride = 16 and Multi Grid = (1; 2; 4), the three convolutions will have rates = 2   (1; 2; 4) = (2; 4; 8) in the block4, respectively."