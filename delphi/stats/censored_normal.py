"""
Censored normal distribution with oracle access (ie. known truncation set)
"""

import torch as ch
from torch import Tensor
from torch.distributions.multivariate_normal import MultivariateNormal
from cox.utils import Parameters
import config

from .stats import stats
from ..oracle import oracle
from ..train import train_model
from ..utils.datasets import DataSet, CENSORED_MULTIVARIATE_NORMAL_REQUIRED_ARGS,\
    CENSORED_MULTIVARIATE_NORMAL_OPTIONAL_ARGS, CensoredNormal
from ..utils import defaults
from ..utils.helpers import Bounds, censored_sample_nll


class censored_normal(stats):
    """
    Censored normal distribution class.
    """
    def __init__(self,
                 phi: oracle,
                 alpha: Tensor,
                 args: Parameters,
                 **kwargs):
        """
        Args:
            
        """
        super(censored_normal, self).__init__()
        # check algorithm hyperparameters
        config.args = defaults.check_and_fill_args(args, defaults.CENSOR_ARGS, CensoredNormal)
        # add oracle and survival prob to parameters
        config.args.__setattr__('phi', phi)
        config.args.__setattr__('alpha', alpha)
        config.args.__setattr__('device', device)
        self._normal = None
        # intialize loss function and add custom criterion to hyperparameters
        self.criterion = CensoredMultivariateNormalNLL.apply
        config.args.__setattr__('custom_criterion', self.criterion)
        # create instance variables for empirical estimates
        self.emp_loc, self.emp_var = None, None
        # initialize projection set
        self.projection_set = None

    def fit(self, S: Tensor):
        """
        """
        # create dataset and dataloader
        ds_kwargs = {
            'custom_class_args': {
                'S': S},
            'custom_class': CensoredNormal,
            'transform_train': None,
            'transform_test': None,
            'label_mapping': None}
        ds = DataSet('censored_normal', CENSORED_MULTIVARIATE_NORMAL_REQUIRED_ARGS,
                     CENSORED_MULTIVARIATE_NORMAL_OPTIONAL_ARGS, data_path=None, **ds_kwargs)
        loaders = ds.make_loaders(workers=config.args.workers, batch_size=config.args.batch_size)
        # get empirical estimates from dataset and initialize distribution
        self._normal = MultivariateNormal(loaders[0].dataset.loc, loaders[0].dataset.var)
        # initialize model with empirical estimates
        self._normal.loc.requires_grad, self._normal.covariance_matrix.requires_grad = True, True
        # initialize projection set and add iteration hook to hyperparameters
        self.projection_set = CensoredNormalProjectionSet(self.emp_loc, self.emp_var)
        config.args.__setattr__('iteration_hook', self.projection_set)
        # run PGD to predict actual estimates
        return train_model(config.args, self._normal, loaders,
                           update_params=[self._normal.loc, self._normal.covariance_matrix])


class CensoredMultivariateNormalNLL(ch.autograd.Function):
    """
    Computes the negative population log likelihood for censored multivariate normal distribution.
    """

    @staticmethod
    def forward(ctx, loc, covariance_matrix, x):
        ctx.save_for_backward(loc, covariance_matrix, x)
        return ch.zeros(1)

    @staticmethod
    def backward(ctx, grad_output):
        loc, covariance_matrix, x = ctx.saved_tensors
        # reparameterize distribution
        T = covariance_matrix.inverse()
        v = T.matmul(loc.unsqueeze(1)).flatten()
        # rejection sampling
        y = Tensor([])
        M = MultivariateNormal(v, T)
        while y.size(0) < x.size(0):
            s = M.sample(sample_shape=ch.Size([config.args.num_samples, ]))
            y = ch.cat([y, s[config.args.phi(s).nonzero(as_tuple=False).flatten()]])
        # calculate gradient
        grad = (-x + censored_sample_nll(y[:x.size(0)])).mean(0)
        return grad[loc.size(0) ** 2:], grad[:loc.size(0) ** 2].reshape(covariance_matrix.size()), None


class CensoredNormalProjectionSet:
    """
    Censored normal distribution projection set
    """
    def __init__(self, emp_loc, emp_scale):
        """
        Args:
            emp_loc (torch.Tensor): empirical mean
            emp_scale (torch.Tensor): empirical variance
        """
        self.emp_loc = emp_loc.clone().detach()
        self.emp_scale = emp_scale.clone().detach()
        self.radius = config.args.radius*(ch.log(1.0/config.args.alpha)/ch.square(config.args.alpha))
        # parameterize projection set
        if config.args.clamp:
            self.loc_bounds, self.scale_bounds = Bounds(self.emp_loc-self.radius, self.emp_loc+self.radius), \
             Bounds(ch.max(ch.square(config.args.alpha/12.0), self.emp_scale - self.radius), self.emp_scale + self.radius)
        else:
            pass

    def __call__(self, M, i, loop_type, inp, target):
        if config.args.clamp:
            M.loc.data = ch.clamp(M.loc.data, float(self.loc_bounds.lower), float(self.loc_bounds.upper))
            M.covariance_matrix.data = ch.clamp(M.covariance_matrix.data, float(self.scale_bounds.lower), float(self.scale_bounds.upper))
        else:
            pass