import sys

sys.path.append('Standard_classifier')

import os
import numpy as np
import matplotlib.pyplot as plt
import copy
import re

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models

import seruso_datasets

from torchvision import transforms

from inference_cam_generation import Cam_generator_inference

from general_utils.augment_utils import *
from general_utils.frame_utils import *
from general_utils.torch_utils import *
from general_utils.cam_utils import *
from general_utils.io_utils import *
from general_utils.log_utils import *

from gradCAM_core.pytorch_grad_cam import * #GradCAM
from gradCAM_core.pytorch_grad_cam.metrics.cam_mult_image import CamMultImageConfidenceChange
from gradCAM_core.pytorch_grad_cam.utils.model_targets import *
from gradCAM_core.pytorch_grad_cam.utils.image import *
from gradCAM_core.pytorch_grad_cam.sobel_cam import sobel_cam
from gradCAM_core.pytorch_grad_cam.metrics.road import *


class Std_classifier_inference(Cam_generator_inference):

    def __init__(self, param1, param2):
        super().__init__(param1, param2)
        self.test_dataset.do_it_without_flows()

    def set_log(self):
        self.log_dir = create_directory(f'./experiments/GradCAM/log/inference/')
        self.cam_dir = create_directory(f'./experiments/GradCAM/cams/')     #  /mnt/datasets_1/andream99/GradCAM/cams/     ./experiments/GradCAM/cams/
        self.log_func = lambda string='': print(string)
    
    def set_model(self):

        model_path = './experiments/GradCAM/models/' + f'{self.tag}.pth'

        # Using regular expression to find the substring
        match = re.search(r'epochs_(.*?)_batch', self.tag)
        architecture = match.group(1)

        loaded_dict = torch.load(model_path)

        self.num_of_classes = loaded_dict['0.fc.bias'].shape[0]

        # loads a pre-trained ResNet-50 model from the torchvision library. The True argument means that the model will be loaded with pre-trained weights.
        self.cam_model = getattr(models, self.architecture)(pretrained=True)

        # extracts the number of input features (or neurons) in the last fully connected layer (fc) of the ResNet-50 model.
        num_ftrs = self.cam_model.fc.in_features

        # replaces the fully connected layer (fc) of the ResNet-50 model with a new linear layer (nn.Linear) that has num_ftrs input features and 2 output features.
        self.cam_model.fc = nn.Linear(num_ftrs, self.num_of_classes)

        #  creates a new sequential model that consists of the modified ResNet-50 and a softmax layer (nn.Softmax). The softmax layer is used to convert the model's output into probabilities for each class.
        self.cam_model = nn.Sequential(self.cam_model, nn.Softmax())

        # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # self.cam_model = self.cam_model.to(device)

        self.cam_model.cuda()
        self.cam_model.eval()

        self.log_func('[i] Architecture is {}'.format(architecture))
        self.log_func('[i] Total Params: %.2fM'%(calculate_parameters(self.cam_model)))
        self.log_func()

        try:
            use_gpu = os.environ['CUDA_VISIBLE_DEVICES']
        except KeyError:
            use_gpu = '0'

        the_number_of_gpu = len(use_gpu.split(','))
        if the_number_of_gpu > 1:
            self.log_func('[i] the number of gpu : {}'.format(the_number_of_gpu))
            self.cam_model = nn.DataParallel(self.cam_model)
            loaded_dict = self.adjust_state_dict(loaded_dict, remove_module=False)

        self.log_func(f'model_path: {model_path}')

        self.cam_model.load_state_dict(loaded_dict)
        
        if the_number_of_gpu > 1:
            self.target_layers = [self.cam_model.module[0].layer4[-1]]

        else:
            self.target_layers = [self.cam_model[0].layer4[-1]]


        

    def prepare_image(self, img, factor, gray = False):

        if gray:
            img = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) , (int(img.shape[1]/factor), int(img.shape[0]/factor)))

        else:
            img = cv2.resize(img, (int(img.shape[1]/factor), int(img.shape[0]/factor)))
            img = np.float32(img)/255

            # img = np.float32(img)/255

        return img

    def CAM_attribution(self, img, factor, label, eigen_smooth= False, aug_smooth=False):

        img = self.prepare_image(img, 1/factor)

        img = preprocess_image(img, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

        # img = torch.from_numpy(img)
        # flipped_image = img.flip(-1)

        # input_tensor = torch.stack([img, flipped_image])
        # input_tensor = input_tensor.cuda()

        # Move the model and input tensor to GPU
        img = img.cuda()

        # Set the random seed
        np.random.seed(42)

        cam_method = GradCAM(model=self.cam_model, target_layers=self.target_layers) #, use_cuda=True)

        # cam_metric = ROADCombined(percentiles=[20, 40, 60, 80])

        targets = [ClassifierOutputTarget(label)]
        # metric_targets = [ClassifierOutputSoftmaxTarget(label)]

        # percentiles = [10, 50, 90]

        with cam_method:
            attributions = cam_method(input_tensor=img, targets=targets, eigen_smooth=eigen_smooth, aug_smooth=aug_smooth)

        # print(f"attributions.shape  {attributions.shape}")
        attribution = attributions[0, :]

        # visualization = show_cam_on_image(img, attribution, use_rgb=True)

        return torch.from_numpy(attribution).unsqueeze(0).unsqueeze(0).float() # attribution 

    
    def generate_cams_with_std_method(self, ori_image, scales, normalize = True):
            
        ori_w, ori_h = ori_image.shape[0], ori_image.shape[1]
        
        strided_size = get_strided_size((ori_h, ori_w), 4)
        strided_up_size = get_strided_up_size((ori_h, ori_w), 16)

        hr = []
        for i in range(self.num_of_classes):

            cams_list = [self.CAM_attribution(np.array(ori_image), scale, i) for scale in scales]
            
            hr_cams_list = [resize_for_tensors(cams, strided_up_size)[0] for cams in cams_list]

            # print(f"hr_cams_list:\t{hr_cams_list}")

            # hr_cams_list = [resize_for_tensors(cams.unsqueeze(0), strided_up_size)[0] for cams in cams_list]
            hr_cam = torch.sum(torch.stack(hr_cams_list), dim=0)[:, :ori_h, :ori_w]

            if normalize:
                hr_cam /= F.adaptive_max_pool2d(hr_cam, (1, 1)) + 1e-5

            hr.append(hr_cam.squeeze(0))

        return hr

        


    def make_all_cams(self, visualize = False, max_item = 10):

        # with torch.no_grad():

            if self.test_dataset.get_whith_mask_bool():
                    
                for index_for_dataset in range(len(self.test_dataset)):
                    
                    sample, mask, path = self.test_dataset[index_for_dataset]

                    hi_res_cams  = self.generate_cams_with_std_method(sample, self.scales, normalize = True)
                    mask = self.generate_masks(hi_res_cams, sample, mask, visualize = visualize)
                    if not visualize:
                        self.save_masks(mask, path)
                    else:
                        if index_for_dataset > max_item:
                            break
                
            else:

                for index_for_dataset in range(len(self.test_dataset)):
                    sample, path  = self.test_dataset[index_for_dataset]
                    hi_res_cams  = self.generate_cams_with_std_method(sample, self.scales, normalize = True)
                    mask = self.generate_masks(hi_res_cams, sample, visualize = visualize)
                    if not visualize:
                        self.save_masks(mask, path)
                    else:
                        if index_for_dataset > max_item:
                            break


