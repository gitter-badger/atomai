"""
trainer.py
========

Module for training fully convolutional neural networs
for atom/defect/particle finding and encoder-decoder neural networks
for prediction of spectra/images from images/spectra.

Created by Maxim Ziatdinov (email: maxim.ziatdinov@ai4microscopy.com)

"""


import copy
import warnings
from collections import OrderedDict
from typing import List, Optional, Tuple, Type, Union

import numpy as np
import torch
from atomai import losses_metrics
from atomai.nets import init_fcnn_model, init_imspec_model
from atomai.transforms import datatransform, unsqueeze_channels
from atomai.utils import (average_weights, check_signal_dims, dummy_optimizer,
                          gpu_usage_map, init_fcnn_dataloaders,
                          init_imspec_dataloaders, array2list, plot_losses,
                          preprocess_training_image_data, set_train_rng)
from sklearn.model_selection import train_test_split

warnings.filterwarnings("ignore", module="torch.nn.functional")


class BaseTrainer:
    """
    Base trainer class for training semantic segmentation
    and image-to-spectrum/spectrum-to-image deep learning models
    """
    def __init__(self):
        """
        Initializes trainer parameters
        """
        set_train_rng(1)
        self.net = torch.nn.Module()
        self.criterion = None
        self.optimizer = None
        self.compute_accuracy = False
        self.full_epoch = True
        self.swa = False
        self.perturb_weights = False
        self.running_weights = {}
        self.training_cycles = 0
        self.batch_idx_train, self.batch_idx_test = [], []
        self.batch_size = 1
        self.nb_classes = None
        self.X_train, self.y_train = None, None
        self.X_test, self.y_test = None, None
        self.train_loader = torch.utils.data.TensorDataset()
        self.test_loader = torch.utils.data.TensorDataset()
        self.augdict = None
        self.filename = "model"
        self.print_loss = 1
        self.meta_state_dict = dict()
        self.loss_acc = {"train_loss": [], "test_loss": [],
                         "train_accuracy": [], "test_accuracy": []}

    def set_model(self,
                  model: Type[torch.nn.Module],
                  nb_classes: int = None) -> None:
        self.net = model
        self.nb_classes = nb_classes

    def set_optimizer(self,
                      optimizer: Optional[Type[torch.optim.Optimizer]] = None
                      ) -> None:
        if optimizer is None:
            self.optimizer = torch.optim.Adam(self.net.parameters(), lr=1e-3)
        else:
            self.optimizer = optimizer
    
    def set_data(self,
                 X_train: torch.Tensor,
                 y_train: torch.Tensor,
                 X_test: torch.Tensor,
                 y_test: torch.Tensor,
                 batch_size: int = 32) -> None:

        for x in [X_train, y_train, X_test, y_test]:
            if not isinstance(x, torch.Tensor):
                raise TypeError("Training data must be torch tensors")

        if self.full_epoch:
            self.train_loader = torch.utils.data.DataLoader(
                torch.utils.data.TensorDataset(X_train, y_train),
                batch_size=batch_size, shuffle=True, drop_last=True)
            self.test_loader = torch.utils.data.DataLoader(
                torch.utils.data.TensorDataset(X_test, y_test),
                batch_size=batch_size, drop_last=True)
        else:
            (self.X_train, self.y_train,
             self.X_test, self.y_test) = array2list(
                X_train, y_train, X_test, y_test, batch_size)

    def loss_fn(self, loss: str = 'mse', nb_classes: int = None) -> None:
        return losses_metrics.select_seg_loss(loss, nb_classes)

    def compile_trainer(self,
                        train_data: Tuple[np.ndarray],
                        loss: str = 'ce',
                        optimizer: Type[torch.optim.Optimizer] = None,
                        training_cycles: int = 1000,
                        batch_size: int = 32,
                        compute_accuracy: bool = False,
                        full_epoch: bool = True,
                        swa: bool = False,
                        perturb_weights: bool = False,
                        **kwargs):
        """
        Compile model for training
        """
        self.full_epoch = full_epoch
        self.training_cycles = training_cycles
        self.compute_accuracy = compute_accuracy
        self.swa = swa
        self.set_data(*train_data, batch_size)

        self.perturb_weights = perturb_weights
        if self.perturb_weights:
            if self.meta_state_dict["batchnorm"]:
                raise AssertionError(
                    "To use time-dependent weights perturbation, " +
                    "turn off the batch normalization layes")
            if isinstance(self.perturb_weights, bool):
                e_p = 1 if self.full_epoch else 50
                self.perturb_weights = {"a": .01, "gamma": 1.5, "e_p": e_p}

        self.set_optimizer(optimizer)
        self.criterion = self.loss_fn(loss, self.nb_classes)

        if not self.full_epoch:
            self.batch_idx_train = np.random.randint(
                0, len(self.X_train), self.training_cycles)
            self.batch_idx_test = np.random.randint(
                0, len(self.X_test), self.training_cycles)

        self.print_loss = kwargs.get("print_loss")
        if self.print_loss is None:
            if not self.full_epoch:
                self.print_loss = 100
            else:
                self.print_loss = 1
        self.filename = kwargs.get("filename", "./model")
        self.plot_training_history = kwargs.get("plot_training_history", True)

    def train_step(self, feat: torch.Tensor, tar: torch.Tensor) -> Tuple[float]:
        """
        Propagates image(s) through a network to get model's prediction
        and compares predicted value with ground truth; then performs
        backpropagation to compute gradients and optimizes weights.
        """
        self.net.train()
        self.optimizer.zero_grad()
        prob = self.net(feat)
        loss = self.criterion(prob, tar)
        loss.backward()
        self.optimizer.step()
        if self.compute_accuracy:
            acc_score = self.accuracy_fn(tar, prob)
            return (loss.item(), acc_score)
        return (loss.item(),)

    def test_step(self, feat: torch.Tensor, tar: torch.Tensor) -> float:
        """
        Forward pass for test data with deactivated autograd engine
        """
        self.net.eval()
        with torch.no_grad():
            prob = self.net(feat)
            loss = self.criterion(prob, tar)
        if self.compute_accuracy:
            acc_score = self.accuracy_fn(tar, prob)
            return (loss.item(), acc_score)
        return (loss.item(),)

    def step(self, e: int) -> None:
        """
        Single train-test step which passes a single
        mini-batch (for both training and testing), i.e.
        1 "epoch" = 1 mini-batch
        """
        features, targets = self.dataloader(
            self.batch_idx_train[e], mode='train')
        # Training step
        loss = self.train_step(features, targets)
        self.loss_acc["train_loss"].append(loss[0])
        features_, targets_ = self.dataloader(
            self.batch_idx_test[e], mode='test')
        # Test step
        loss_ = self.test_step(features_, targets_)
        self.loss_acc["test_loss"].append(loss_[0])
        if self.compute_accuracy:
            self.loss_acc["train_accuracy"].append(loss[1])
            self.loss_acc["test_accuracy"].append(loss_[1])

    def step_full(self) -> None:
        """
        A standard PyTorch training loop where
        all available mini-batches are passed at
        a single step/epoch
        """
        c, c_test = 0, 0
        losses, losses_test = 0, 0
        if self.compute_accuracy:
            acc, acc_test = 0, 0
        # Training step
        for features, targets in self.train_loader:
            loss = self.train_step(features, targets)
            losses += loss[0]
            if self.compute_accuracy:
                acc += loss[1]
            c += 1
        else:  # Test step
            for features_, targets_ in self.test_loader:
                loss_ = self.test_step(features_, targets_)
                losses_test += loss_[0]
                if self.compute_accuracy:
                    acc_test += loss_[1]
                c_test += 1
        self.loss_acc["train_loss"].append(losses / c)
        self.loss_acc["test_loss"].append(losses_test / c_test)
        if self.compute_accuracy:
            self.loss_acc["train_accuracy"].append(acc / c)
            self.loss_acc["test_accuracy"].append(acc_test / c_test)

    def eval_model(self) -> None:
        """
        Evaluates model on the entire dataset
        """
        self.net.eval()
        running_loss_test, c = 0, 0
        if self.compute_accuracy:
            running_acc_test = 0
        if self.full_epoch:
            for features_, targets_ in self.test_loader:
                loss_ = self.test_step(features_, targets_)
                running_loss_test += loss_[0]
                if self.compute_accuracy:
                    running_acc_test += loss_[1]
                c += 1
            print('Model (final state) evaluation loss:',
                  np.around(running_loss_test / c, 4))
            if self.iou:
                print('Model (final state) IoU:',
                      np.around(running_acc_test / c, 4))
        else:
            running_loss_test, running_acc_test = 0, 0
            for idx in range(len(self.X_test)):
                features_, targets_ = self.dataloader(idx, mode='test')
                loss_ = self.test_step(features_, targets_)
                running_loss_test += loss_[0]
                if self.compute_accuracy:
                    running_acc_test += loss_[1]
            print('Model (final state) evaluation loss:',
                  np.around(running_loss_test / len(self.X_test), 4))
            if self.compute_accuracy:
                print('Model (final state) IoU:',
                      np.around(running_acc_test / len(self.X_test), 4))

    def save_model(self, *args: str) -> None:
        try:
            filename = args[0]
        except IndexError:
            filename = self.filename
        torch.save(self.meta_state_dict,
                   filename + '.tar')

    def print_statistics(self, e: int, **kwargs) -> None:
        """
        Print loss and (optionally) IoU score on train
        and test data, as well as GPU memory usage.
        """
        accuracy_metrics = kwargs.get("accuracy_metrics", "Accuracy")
        if torch.cuda.is_available():
            gpu_usage = gpu_usage_map(torch.cuda.current_device())
        else:
            gpu_usage = ['N/A ', ' N/A']
        if self.compute_accuracy:
            print('Epoch {} ...'.format(e+1),
                  'Training loss: {} ...'.format(
                      np.around(self.loss_acc["train_loss"][-1], 4)),
                  'Test loss: {} ...'.format(
                      np.around(self.loss_acc["test_loss"][-1], 4)),
                  'Train {}: {} ...'.format(
                      accuracy_metrics,
                      np.around(self.loss_acc["train_accuracy"][-1], 4)),
                  'Test {}: {} ...'.format(
                      accuracy_metrics,
                      np.around(self.loss_acc["test_accuracy"][-1], 4)),
                  'GPU memory usage: {}/{}'.format(
                      gpu_usage[0], gpu_usage[1]))
        else:
            print('Epoch {} ...'.format(e+1),
                  'Training loss: {} ...'.format(
                      np.around(self.loss_acc["train_loss"][-1], 4)),
                  'Test loss: {} ...'.format(
                      np.around(self.loss_acc["test_loss"][-1], 4)),
                  'GPU memory usage: {}/{}'.format(
                      gpu_usage[0], gpu_usage[1]))

    def accuracy_fn(self, y, y_prob) -> None:
        """
        Computes accuracy score
        """
        pass

    def dataloader(self, batch_num: int,
                   mode: str = 'train') -> Tuple[torch.Tensor]:
        """
        Generates input training data with images/spectra
        and the associated labels (spectra/images)
        """
        if mode == 'test':
            features = self.X_test[batch_num][:self.batch_size]
            targets = self.y_test[batch_num][:self.batch_size]
        else:
            features = self.X_train[batch_num][:self.batch_size]
            targets = self.y_train[batch_num][:self.batch_size]
        if torch.cuda.is_available():
            features, targets = features.cuda(), targets.cuda()
        return features, targets

    def weight_perturbation(self, e: int) -> None:
        raise NotImplementedError

    def save_running_weights(self, e: int) -> None:
        """
        Saves running weights (for stochastic weights averaging)
        """
        swa_epochs = 5 if self.full_epoch else 30
        if self.training_cycles - e <= swa_epochs:
            i_ = swa_epochs - (self.training_cycles - e)
            state_dict_ = OrderedDict()
            for k, v in self.net.state_dict().items():
                state_dict_[k] = copy.deepcopy(v).cpu()
            self.running_weights[i_] = state_dict_
        return

    def fit(self, **kwargs) -> Type[torch.nn.Module]:
        """
        Trains a neural network, prints the statistics,
        saves the final model weights.
        """
        auglist = ["custom_transform", "zoom", "gauss_noise", "jitter",
                   "poisson_noise", "contrast", "salt_and_pepper", "blur",
                   "resize", "rotation", "background"]
        self.augdict = {k: kwargs[k] for k in auglist if k in kwargs.keys()}

        for e in range(self.training_cycles):
            if self.full_epoch:
                self.step_full()
            else:
                self.step(e)
            if self.swa:
                self.save_running_weights(e)
            if self.perturb_weights:
                self.weight_perturbation(e)
            if e == 0 or (e+1) % self.print_loss == 0:
                self.print_statistics(e, accuracy_metrics="IoU")
        self.save_model(self.filename + "_metadict_final")
        if not self.full_epoch:
            self.eval_model()
        if self.swa:
            print("Performing stochastic weights averaging...")
            self.net.load_state_dict(average_weights(self.running_weights))
            self.eval_model()
        if self.plot_training_history:
            plot_losses(self.loss_acc["train_loss"],
                        self.loss_acc["test_loss"])


class SegTrainer(BaseTrainer):
    """
    Class for training a fully convolutional neural network
    for semantic segmentation of noisy experimental data

    Args:
        X_train (numpy array):
            4D numpy array (3D image tensors stacked along the first dim)
            representing training images
        y_train (list or dict or 4D numpy array):
            4D (binary) / 3D (multiclass) numpy array
            where 3D / 2D images stacked along the first array dimension
            represent training labels (aka masks aka ground truth).
            The reason why in the multiclass case the images are 4-dimensional
            tensors and the labels are 3-dimensional tensors is because of how
            the cross-entropy loss is calculated in PyTorch
            (see https://pytorch.org/docs/stable/nn.html#nllloss).
        X_test (list or dict or 4D numpy array):
            4D numpy array (3D image tensors stacked along the first dim)
            representing test images
        y_test (list or dict or 4D numpy array):
            4D (binary) / 3D (multiclass) numpy array
            where 3D / 2D images stacked along the first array dimension
            represent test labels (aka masks aka ground truth)
        training_cycles (int):
            Number of training 'epochs' (1 epoch == 1 batch)
        model (str):
            Type of model to train: 'dilUnet' or 'dilnet' (Default: 'dilUnet').
            See atomai.nets for more details. One can also pass a custom fully
            convolutional neural network model.
        compute_accuracy (bool):
            Compute and show mean Intersection over Union for each batch/iteration
            (Default: False)
        seed (int):
            Deterministic mode for model training (Default: 1)
        batch_seed (int):
            Separate seed for generating a sequence of batches
            for training/testing. Equal to 'seed' if set to None (default)
        **batch_size (int):
            Size of training and test batches
        **test_size (float):
            proportion of the dataset (X_train, y_train) for model evaluation;
            used if X_test and/or y_test are not specified (Default: 0.15)
        **use_batchnorm (bool):
            Apply batch normalization after each convolutional layer
            (Default: True)
        **use_dropouts (bool):
            Apply dropouts in the three inner blocks in the middle of a network
            (Default: False)
        **loss (str):
            Type of loss for model training ('ce', 'dice' or 'focal')
            (Default: 'ce')
        **upsampling (str):
            "bilinear" or "nearest" upsampling method (Default: "bilinear")
        **nb_filters (int):
            Number of convolutional filters in the first convolutional block
            (this number doubles in the consequtive block(s),
            see definition of dilUnet and dilnet models for details)
        **with_dilation (bool):
            Use dilated convolutions in the bottleneck of dilUnet
            (Default: True)
        **layers (list):
            List with a number of layers in each block.
            For U-Net the first 4 elements in the list
            are used to determine the number of layers
            in each block of the encoder (including bottleneck layer),
            and the number of layers in the decoder  is chosen accordingly
            (to maintain symmetry between encoder and decoder)
        **swa (bool):
            Saves the last 30 stochastic weights that can be averaged later on
        **perturb_weights (bool or dict):
            Time-dependent weight perturbation, :math:`w\\leftarrow w + a / (1 + e)^\\gamma`,
            where parameters *a* and *gamma* can be passed as a dictionary,
            together with parameter *e_p* determining every n-th epoch at
            which a perturbation is applied
        **print_loss (int):
            Prints loss every *n*-th epoch
        **filename (str):
            Filename for model weights
            (appended with "_test_weights_best.pt" and "_weights_final.pt")
        **plot_training_history (bool):
            Plots training and test curves vs epochs at the end of training
        **kwargs:
            One can also pass kwargs for utils.datatransform class
            to perform the augmentation "on-the-fly" (e.g. rotation=True,
            gauss=[20, 60], ...)

    Example:

    >>> # Load 4 numpy arrays with training and test data
    >>> dataset = np.load('training_data.npz')
    >>> images_all = dataset['X_train']
    >>> labels_all = dataset['y_train']
    >>> images_test_all = dataset['X_test']
    >>> labels_test_all = dataset['y_test']
    >>> # Train a model
    >>> t = atomnet.trainer(
    >>>     images_all, labels_all,
    >>>     images_test_all, labels_test_all,
    >>>     training_cycles=500)
    >>> trained_model = t.run()
    """
    def __init__(self,
                 model: str = 'dilUnet',
                 nb_classes: int = 1,
                 seed: int = 1,
                 batch_seed: Optional[int] = None,
                 **kwargs: Union[int, List, str, bool]) -> None:
        """
        Initialize a single FCNN model trainer
        """
        super(SegTrainer, self).__init__()
        
        # Set random seeds and determinism
        set_train_rng(seed)
        if batch_seed is None:
            np.random.seed(seed)
        else:
            np.random.seed(batch_seed)

        self.nb_classes = nb_classes
        self.net, self.meta_state_dict = init_fcnn_model(
                                model, self.nb_classes, **kwargs)
        if torch.cuda.is_available():
            self.net.cuda()
        else:
            warnings.warn(
                "No GPU found. The training can be EXTREMELY slow",
                UserWarning
            )
        #self.optimizer = torch.optim.Adam(self.net.parameters(), lr=1e-3)
        self.meta_state_dict["weights"] = self.net.state_dict()
        self.meta_state_dict["optimizer"] = self.optimizer

    def set_data(self,
                 X_train: np.ndarray,
                 y_train: np.ndarray,
                 X_test: Optional[np.ndarray] = None,
                 y_test: Optional[np.ndarray] = None,
                 **kwargs) -> None:

        if X_test is None or y_test is None:
            X_train, X_test, y_train, y_test = train_test_split(
                X_train, y_train, test_size=kwargs.get("test_size", .15),
                shuffle=True, random_state=kwargs.get("seed", 1))

        self.batch_size = kwargs.get("batch_size", 32)

        if self.full_epoch:
            loaders = init_fcnn_dataloaders(
                X_train, y_train, X_test, y_test, self.batch_size)
            self.train_loader, self.test_loader, nb_classes = loaders
        else:
            (self.X_train, self.y_train,
             self.X_test, self.y_test,
             nb_classes) = preprocess_training_image_data(
                                    X_train, y_train, X_test, y_test,
                                    self.batch_size)
        
        if self.nb_classes != nb_classes:
            raise AssertionError("Number of classes in initialized model" +
                                 " is different from the number of classes" +
                                 " contained in training data")

    def dataloader(self,
                   batch_num: int,
                   mode: str = 'train') -> Tuple[torch.Tensor]:
        """
        Generates 2 batches of 4D tensors (images and masks)
        """
        # Generate batch of training images with corresponding ground truth
        if mode == 'test':
            images = self.X_test[batch_num][:self.batch_size]
            labels = self.y_test[batch_num][:self.batch_size]
        else:
            images = self.X_train[batch_num][:self.batch_size]
            labels = self.y_train[batch_num][:self.batch_size]
        # "Augment" data if applicable
        if len(self.augdict) > 0:
            dt = datatransform(
                self.num_classes, "channel_first", 'channel_first',
                True, len(self.loss_acc["train_loss"]), **self.augdict)
            images, labels = dt.run(
                images[:, 0, ...], unsqueeze_channels(labels, self.nb_classes))
        # Transform images and ground truth to torch tensors and move to GPU
        images = torch.from_numpy(images).float()
        if self.nb_classes == 1:
            labels = torch.from_numpy(labels).float()
        else:
            labels = torch.from_numpy(labels).long()
        if torch.cuda.is_available():
            images, labels = images.cuda(), labels.cuda()
        return images, labels

    def accuracy_fn(self, y, y_prob, *args):
        iou_score = losses_metrics.IoU(
                y, y_prob, self.nb_classes).evaluate()
        return iou_score

    def weight_perturbation(self, e: int) -> None:
        """
        Time-dependent weights perturbation
        (role of time is played by "epoch" number)
        """
        a = self.perturb_weights["a"]
        gamma = self.perturb_weights["gamma"]
        e_p = self.perturb_weights["e_p"]
        if self.perturb_weights and (e + 1) % e_p == 0:
            var = torch.tensor(a / (1 + e)**gamma)
            for k, v in self.net.state_dict().items():
                v_prime = v + v.new(v.shape).normal_(0, torch.sqrt(var))
                self.net.state_dict()[k].copy_(v_prime)
        return

    '''def fit_(self,
            X_train: np.ndarray,
            y_train: np.ndarray,
            X_test: Optional[np.ndarray] = None,
            y_test: Optional[np.ndarray] = None,
            training_cycles: int = 1000,
            loss: str = "ce",
            compute_accuracy: bool = False,
            full_epoch: bool = False,
            swa: bool = False,
            **kwargs) -> Type[torch.nn.Module]:
        """
        Trains a neural network, prints the statistics,
        saves the final model weights.
        """
        train_data = (X_train, y_train, X_test, y_test)
        loss = losses_metrics.select_seg_loss(loss, self.nb_classes)
        self.compile_trainer(
            train_data, loss, self.optimizer, training_cycles, compute_accuracy,
            full_epoch, swa, **kwargs)

        auglist = ["custom_transform", "zoom", "gauss_noise", "jitter",
                       "poisson_noise", "contrast", "salt_and_pepper", "blur",
                       "resize", "rotation", "background"]
        self.augdict = {k: kwargs[k] for k in auglist if k in kwargs.keys()}

        for e in range(self.training_cycles):
            if self.full_epoch:
                self.step_full()
            else:
                self.step(e)
            if swa:
                self.save_running_weights(e)
            if self.perturb_weights:
                self.weight_perturbation(e)
            if e == 0 or (e+1) % self.print_loss == 0:
                self.print_statistics(e, accuracy_metrics="IoU")
        self.save_model(self.filename + "_metadict_final")
        if not self.full_epoch:
            self.eval_model()
        if swa:
            print("Performing stochastic weights averaging...")
            self.net.load_state_dict(average_weights(self.running_weights))
            self.eval_model()
        if self.plot_training_history:
            plot_losses(self.loss_acc["train_loss"],
                        self.loss_acc["test_loss"])
        return self.net'''


class ImSpecTrainer(BaseTrainer):
    """
    Trainer of neural network for image-to-spectrum
    and spectrum-to-image transformations

    Args:
        X_train (numpy array):
            4D numpy array with image data (n_samples x 1 x height x width)
            or 3D numpy array with spectral data (n_samples x 1 x signal_length).
            It is also possible to pass 3D and 2D arrays by ignoring the channel dim,
            which will be added automatically.
        y_train (numpy array):
            3D numpy array with spectral data (n_samples x 1 x signal_length)
            or 4D numpy array with image data (n_samples x 1 x height x width).
            It is also possible to pass 2D and 3D arrays by ignoring the channel dim,
            which will be added automatically. Note that if your X_train data are images,
            then your y_train must be spectra and vice versa.
        X_test (list or dict or 4D numpy array):
            4D numpy array with image data (n_samples x 1 x height x width)
            or 3D numpy array with spectral data (n_samples x 1 x signal_length).
            It is also possible to pass 3D and 2D arrays by ignoring the channel dim,
            which will be added automatically.
        y_test (list or dict or 4D numpy array):
            3D numpy array with spectral data (n_samples x 1 x signal_length)
            or 4D numpy array with image data (n_samples x 1 x height x width).
            It is also possible to pass 2D and 3D arrays by ignoring the channel dim,
            which will be added automatically. Note that if your X_train data are images,
            then your y_train must be spectra and vice versa.
        latent_dim (int):
            dimensionality of the latent space
            (number of neurons in a fully connected bottleneck layer)
        training_cycles (int):
            Number of training 'epochs' (1 epoch == 1 batch)
        seed (int):
            Deterministic mode for model training (Default: 1)
        batch_seed (int):
            Separate seed for generating a sequence of batches
            for training/testing. Equal to 'seed' if set to None (default)
        **batch_size (int):
            Size of training and test batches
        **test_size (float):
            proportion of the dataset (X_train, y_train) for model evaluation;
            used if X_test and/or y_test are not specified (Default: 0.15)
        **nblayers_encoder (int):
            number of convolutional layers in the encoder
        **nblayers_decoder (int):
            number of convolutional layers in the decoder
        **nbfilters_encoder (int):
            number of convolutional filters in each layer of the encoder
        **nbfilters_decoder (int):
            number of convolutional filters in each layer of the decoder
        **use_batchnorm (bool):
            Apply batch normalization after each convolutional layer
            (Default: True)
        **encoder_downsampling (int):
            downsamples input data by this factor before passing
            to convolutional layers (Default: no downsampling)
        **decoder_upsampling (bool):
            performs upsampling+convolution operation twice on the reshaped latent
            vector (starting from image/spectra dims 4x smaller than the target dims)
            before passing  to the decoder
        **swa (bool):
            Saves the last 30 stochastic weights that can be averaged later on
        **print_loss (int):
            Prints loss every *n*-th epoch
        **filename (str):
            Filename for model weights
            (appended with "_test_weights_best.pt" and "_weights_final.pt")
        **plot_training_history (bool):
            Plots training and test curves vs epochs at the end of training

    Example:

    >>> # Load 4 numpy arrays with training and test data
    >>> dataset = np.load('training_data.npz')
    >>> images_all = dataset['X_train']
    >>> labels_all = dataset['y_train']
    >>> images_test_all = dataset['X_test']
    >>> labels_test_all = dataset['y_test']
    >>> # Train a model
    >>> t = atomnet.trainer(
    >>>     images_all, labels_all,
    >>>     images_test_all, labels_test_all,
    >>>     training_cycles=500)
    >>> trained_model = t.run()
    """
    def __init__(self,
                 X_train: np.ndarray,
                 y_train: np.ndarray,
                 X_test: Optional[np.ndarray] = None,
                 y_test: Optional[np.ndarray] = None,
                 latent_dim: int = 2,
                 training_cycles: int = 1000,
                 seed: int = 1,
                 batch_seed: Optional[int] = None,
                 **kwargs: Union[int, bool, str]) -> None:
        super(ImSpecTrainer, self).__init__()
        """
        Initialize trainer's parameters
        """
        set_train_rng(seed)
        if batch_seed is None:
            np.random.seed(seed)
        else:
            np.random.seed(batch_seed)
        if X_test is None or y_test is None:
            X_train, X_test, y_train, y_test = train_test_split(
                X_train, y_train, test_size=kwargs.get("test_size", .15),
                shuffle=True, random_state=seed)
        X_train, y_train, X_test, y_test = check_signal_dims(
            X_train, y_train, X_test, y_test)
        in_dim = X_train.shape[2:]
        out_dim = y_train.shape[2:]
        self.batch_size = kwargs.get("batch_size", 32)
        self.full_epoch = kwargs.get("full_epoch")
        if self.full_epoch:
            self.train_loader, self.test_loader = init_imspec_dataloaders(
                X_train, y_train, X_test, y_test, self.batch_size)
        else:
            (self.X_train, self.y_train,
             self.X_test, self.y_test) = ndarray2list(
                X_train, y_train, X_test, y_test, self.batch_size)

        if not self.full_epoch:
            self.batch_idx_train = np.random.randint(
                0, len(self.X_train), training_cycles)
            self.batch_idx_test = np.random.randint(
                0, len(self.X_test), training_cycles)

        (self.net,
         self.meta_state_dict) = init_imspec_model(in_dim, out_dim, latent_dim,
                                                   **kwargs)
        if torch.cuda.is_available():
            self.net.cuda()
        self.optimizer = torch.optim.Adam(self.net.parameters(), lr=1e-3)
        self.criterion = torch.nn.MSELoss()
        self.swa = kwargs.get("swa", False)
        self.training_cycles = training_cycles
        self.print_loss = kwargs.get("print_loss")
        if self.print_loss is None:
            if not self.full_epoch:
                self.print_loss = 100
            else:
                self.print_loss = 1
        self.filename = kwargs.get("filename", "./model")
        self.plot_training_history = kwargs.get("plot_training_history", True)
        self.meta_state_dict["weights"] = self.net.state_dict()
        self.meta_state_dict["optimizer"] = self.optimizer

    def dataloader(self, batch_num: int,
                   mode: str = 'train') -> Tuple[torch.Tensor]:
        """
        """
        if mode == 'test':
            features = self.X_test[batch_num][:self.batch_size]
            targets = self.y_test[batch_num][:self.batch_size]
        else:
            features = self.X_train[batch_num][:self.batch_size]
            targets = self.y_train[batch_num][:self.batch_size]
        features = torch.from_numpy(features).float()
        targets = torch.from_numpy(targets).float()
        if torch.cuda.is_available():
            features, targets = features.cuda(), targets.cuda()
        return features, targets

    def run(self) -> Type[torch.nn.Module]:
        """
        Trains a neural network, prints the statistics,
        saves the final model weights.
        """
        for e in range(self.training_cycles):
            if self.full_epoch:
                self.step_full()
            else:
                self.step(e)
            if self.swa:
                self.save_running_weights(e)
            if e == 0 or (e+1) % self.print_loss == 0:
                self.print_statistics(e)
        self.save_model(self.filename + "_metadict_final")
        if not self.full_epoch:
            self.eval_model()
        if self.swa:
            print("Performing stochastic weights averaging...")
            self.net.load_state_dict(average_weights(self.running_weights))
            self.eval_model()
        if self.plot_training_history:
            plot_losses(self.loss_acc["train_loss"],
                        self.loss_acc["test_loss"])
        return self.net
