import torch
import torch.nn as nn
import torchvision.models as models
import torchvision.datasets as datasets
import torchvision.transforms as transforms
import random
random.seed(4)
# PyTorch TensorBoard support
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
import os
from torch import optim, nn, utils, Tensor
from torchvision.transforms import ToTensor
import lightning as L
import numpy as np
import argparse
from typing import Any
from lightning.pytorch.utilities.types import STEP_OUTPUT
from lightning.pytorch.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger

# You will need to install some of these packages for model training as they are not installed with AnonFair. 

def custom_type(value):
    # Try to convert the value to a number
    try:
        return float(value)
    except ValueError:
        # If it's not a number, check for boolean values
        if value.lower() == 'true':
            return True
        elif value.lower() == 'false':
            return False
        else:
            raise argparse.ArgumentTypeError(f"Invalid value: {value}")

parser = argparse.ArgumentParser(description="Train and test a two-headed model on CelebA dataset.")
parser.add_argument("--target_class", type=int, default=31, help="Index of the target class.")
parser.add_argument("--protected_class", type=int, default=20, help="Index of the protected class.")
parser.add_argument("--limit_train_batches", type=int, default=1000, help="Limit train batches during training.")
parser.add_argument("--limit_val_batches", type=custom_type, default=False, help="Limit validation batches during training.")
parser.add_argument("--max_epochs", type=int, default=20, help="Maximum number of epochs.")
parser.add_argument("--scaling_factor", type=float, default=0.5, help="Scaling factor on second head")
parser.add_argument("--backbone", type=str, default="mobilenetv3", choices=["mobilenetv3", "resnet18", "resnet50"], help="Backbone architecture for the model.")

args = parser.parse_args()

if torch.cuda.is_available():
    print("CUDA is available. Using GPU.")
else:
    print("CUDA is not available. Using CPU.")

#Loading Backbone Pretrained on ImageNet
if args.backbone == "mobilenetv3":
    model = models.mobilenet_v3_small(weights='MobileNet_V3_Small_Weights.IMAGENET1K_V1')
    model.classifier[3] = torch.nn.Linear(1024, 2)
elif args.backbone == "resnet18":
    from torchvision.models import resnet18, ResNet18_Weights
    model = models.resnet18(weights="ResNet18_Weights.DEFAULT")
    model.fc = torch.nn.Linear(512, 2)
elif args.backbone == "resnet50":
    from torchvision.models import resnet50, ResNet50_Weights
    model = models.resnet50(weights="ResNet50_Weights.DEFAULT")
    model.fc = torch.nn.Linear(2048, 2)
else:
    raise ValueError(f"Invalid backbone choice: {args.backbone}")

#####
# Root directory for the dataset
data_root = './data/'

celeba_train = datasets.CelebA(data_root,split='train',
                              download=False,
                              transform=transforms.Compose([ 
                                  transforms.Resize(256),
                                  transforms.RandomCrop(224),
                                  transforms.RandomHorizontalFlip(),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                       std=[0.229, 0.224, 0.225])
                              ]))
celeba_val = datasets.CelebA(data_root, split='valid',
                              download=False,
                              transform=transforms.Compose([
                                  transforms.Resize(256),
                                  transforms.CenterCrop(224),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                       std=[0.229, 0.224, 0.225])
                              ]))

celeba_test = datasets.CelebA(data_root, split='test',
                              download=False,
                              transform=transforms.Compose([
                                  transforms.Resize(256),
                                  transforms.CenterCrop(224),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                       std=[0.229, 0.224, 0.225])
                              ]))

##########

target_loss = torch.nn.BCEWithLogitsLoss()
attribute_loss = torch.nn.MSELoss()
target_class= args.target_class
protected_class= args.protected_class

scaling_factor= args.scaling_factor # factor of 0.5 used by Lohaus et al.

#for inspecting and getting index of attributes
def get_all_celeba_attributes():
    return ['5_o_Clock_Shadow', 'Arched_Eyebrows', 'Attractive', 'Bags_Under_Eyes', 'Bald', 'Bangs', 'Big_Lips',
            'Big_Nose', 'Black_Hair', 'Blond_Hair', 'Blurry', 'Brown_Hair', 'Bushy_Eyebrows', 'Chubby',
            'Double_Chin', 'Eyeglasses', 'Goatee', 'Gray_Hair', 'Heavy_Makeup', 'High_Cheekbones', 'Male',
            'Mouth_Slightly_Open', 'Mustache', 'Narrow_Eyes', 'No_Beard', 'Oval_Face', 'Pale_Skin',
            'Pointy_Nose', 'Receding_Hairline', 'Rosy_Cheeks', 'Sideburns', 'Smiling', 'Straight_Hair',
            'Wavy_Hair', 'Wearing_Earrings', 'Wearing_Hat', 'Wearing_Lipstick', 'Wearing_Necklace',
            'Wearing_Necktie', 'Young']

 
def total_loss(y,pred):
    "Two headed training loss"
    if y.dim() == 1:
        y = y.unsqueeze(0)
    y = y.type(torch.float32)    
    tl = target_loss(pred[:, 0],y[:, target_class])
    al = attribute_loss(pred[:,1],y[:, protected_class])
    return tl + al*scaling_factor

### NB if Making this a Multi-Head Model total loss and logging would be slightly different - See comments at the end

class LitTwoHead(L.LightningModule):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def my_logging(self, loss, y, pred):
        # Logging to TensorBoard (if installed) by default
        self.log("train_loss", loss)
        self.log('head 1 loss', target_loss(pred[:, 0], y[:, target_class].type(torch.float32)))
        self.log('head 2 loss', attribute_loss(pred[:, 1], y[:, protected_class].type(torch.float32)))
        self.log('accuracy (head 1)', ((pred[:, 0] <= 0) == (y[:, target_class] <= 0)).type(torch.float32).mean())
        self.log('accuracy (head 2)', ((pred[:, 1] <= 0.5) == (y[:, protected_class] <= 0)).type(torch.float32).mean())

    def training_step(self, batch, batch_idx):
        # training_step defines the train loop.
        # it is independent of forward
        x, y = batch
        pred = self.model(x)
        loss = total_loss(y, pred)  
        self.my_logging(loss, y, pred)
        return loss

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=0.0001, weight_decay=0)
        return optimizer

    def test_step(self, batch, batch_idx):
        # this is the test loop
        x, y = batch
        pred = self.model(x)
        loss = total_loss(y, pred)  
        self.my_logging(loss, y, pred)

    def validation_step(self, batch, batch_idx):
        # Same statistics as test
        #self.test_step(batch, batch_idx)
        x, y = batch
        pred = self.model(x)
        loss = total_loss(y, pred)
        self.my_logging(loss, y, pred)
        self.log('val_loss', loss)  # Log the validation loss
        
        accuracy_targets_val = ((pred[:, 0] <= 0) == (y[:, target_class] <= 0)).type(torch.float32).mean()
        self.log('accuracy_targets_val', accuracy_targets_val)


if __name__ == "__main__":
    model_trainer = LitTwoHead(model)  

    train_loader = utils.data.DataLoader(celeba_train, batch_size=32, num_workers=9, persistent_workers=True)
    val_loader = utils.data.DataLoader(celeba_val, batch_size=32, num_workers=9, persistent_workers=True)
    test_loader = utils.data.DataLoader(celeba_test, batch_size=32, num_workers=9, persistent_workers=True)


    output_folder = get_all_celeba_attributes()[target_class] + '_' + get_all_celeba_attributes()[protected_class] + '_' + str(args.backbone) + '_prototyping'

    os.makedirs(output_folder, exist_ok=True)

    logger = TensorBoardLogger(output_folder, name="logs")

    checkpoint_callback = ModelCheckpoint(
        monitor='accuracy_targets_val', 
        mode='max', 
        dirpath=output_folder,
        filename='best_model-{epoch:02d}-{accuracy_targets_val:.2f}',
        save_top_k=1
    ) #saving the model based on validation performance on the target attribute

    trainer = L.Trainer(
        limit_train_batches=args.limit_train_batches,
        limit_val_batches=args.limit_val_batches,
        max_epochs=args.max_epochs,
        default_root_dir=output_folder,
        logger=logger,
        callbacks=[checkpoint_callback]
    )

    trainer.fit(model=model_trainer, train_dataloaders=train_loader, val_dataloaders=val_loader)

    trainer = L.Trainer()

    loaded_model = LitTwoHead.load_from_checkpoint(checkpoint_path=checkpoint_callback.best_model_path, model=model)

    loaded_model.eval()
    
    trainer.test(model=loaded_model, dataloaders=test_loader)

#Uncomment the below lines if you want to do forward pass and save model outputs.

    # outputs_val = np.zeros((len(celeba_val), 2))
    # for i, data in enumerate(celeba_val):
    #     outputs_val[i] = loaded_model.model(data[0].unsqueeze(0)).detach()
    # np.save(os.path.join(output_folder, get_all_celeba_attributes()[target_class] + f"_{args.backbone}_weights_" + '_outputs_val.npy'), outputs_val)
    
    # # Save validation target labels and protected labels
    # np.save(os.path.join(output_folder, get_all_celeba_attributes()[target_class] + f"_{args.backbone}_weights_" + '_target_labels_val.npy'), celeba_val.attr[:, target_class].numpy())
    # np.save(os.path.join(output_folder, get_all_celeba_attributes()[target_class] + f"_{args.backbone}_weights_" + '_protected_labels_val.npy'), celeba_val.attr[:, protected_class].numpy())
    
    # outputs_test = np.zeros((len(celeba_test), 2))
    # for i, data in enumerate(celeba_test):
    #     outputs_test[i] = loaded_model.model(data[0].unsqueeze(0)).detach()
    # np.save(os.path.join(output_folder, get_all_celeba_attributes()[target_class] + f"_{args.backbone}_weights_" + '_outputs_test.npy'), outputs_test)
    
    # # Save test target labels and protected labels
    # np.save(os.path.join(output_folder, get_all_celeba_attributes()[target_class] + f"_{args.backbone}_weights_" + '_target_labels_test.npy'), celeba_test.attr[:, target_class].numpy())
    # np.save(os.path.join(output_folder, get_all_celeba_attributes()[target_class] + f"_{args.backbone}_weights_" + '_protected_labels_test.npy'), celeba_test.attr[:, protected_class].numpy())


# What about Multi-Head Models? The set-up is very similar with a few slight changes. 
    #You will need to change the dimension of the linear layer to be consistent with the number of heads (N) --- model.fc = torch.nn.Linear(2048, N) 
    #You will need to adjust the loss and the logging and callbacks follwoing the below code. 

        # def total_loss_multihead(y,pred):
        #     if y.dim() == 1:
        #         y = y.unsqueeze(0)
        #     y = y.type(torch.float32)
        #     al = attribute_loss(pred[:,protected_class],y[:, protected_class]) 
        #     tl = target_loss(pred[:, :protected_class],y[:, :protected_class]).sum()
        #     tl = tl + target_loss(pred[:, protected_class+1:],y[:, protected_class+1:]).sum()
        #     return tl + al*scaling_factor

        # def my_logging_multihead(self, loss, y, pred):
        #     self.log("train_loss", loss)
            
        #     self.log('accuracy (target attributes)', (((pred[:, :protected_class] <= 0) == (y[:, :protected_class] <= 0)).type(torch.float32).mean(0).sum(0) 
        #                                             + ((pred[:, protected_class+1:] <= 0) == (y[:, protected_class+1:] <= 0)).type(torch.float32).mean(0).sum(0))/(pred.shape[1]-1))
        #     self.log('accuracy (protected attribute)', ((pred[:, protected_class] <= 0.5) == (y[:, protected_class] <= 0)).type(torch.float32).mean())
