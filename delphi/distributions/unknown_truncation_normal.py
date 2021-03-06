"""
Truncated normal distribution without oracle access (ie. unknown truncation set)
"""

import torch as ch
from torch import Tensor
from torch.distributions.multivariate_normal import MultivariateNormal
from cox.utils import Parameters
import config

from .stats import stats
from ..oracle import oracle
from ..train import train_model
from ..utils.helpers import Bounds, Exp_h
from ..utils.datasets import TRUNCATED_MULTIVARIATE_NORMAL_REQUIRED_ARGS, TRUNCATED_MULTIVARIATE_NORMAL_OPTIONAL_ARGS, \
    TruncatedNormal, DataSet
from ..grad import TruncatedMultivariateNormalNLL
from ..utils import defaults


class truncated_normal(stats):
    """
    Truncated normal distribution class.
    """
    def __init__(
            self,
            phi: oracle,
            alpha: float,
            args: Parameters,
            **kwargs):
        super(truncated_normal, self).__init__()
        # check algorithm hyperparameters
        config.args = defaults.check_and_fill_args(args, defaults.HERMITE_ARGS, TruncatedNormal)
        # add oracle and survival prob to parameters
        config.args.__setattr__('phi', phi)
        config.args.__setattr__('alpha', alpha)
        self._normal = None
        # intialize loss function and add custom criterion to hyperparameters
        self.criterion = TruncatedMultivariateNormalNLL.apply
        config.args.__setattr__('custom_criterion', self.criterion)

    def fit(self, S: Tensor):
        """
        :param S:
        :return:
        """
        # create dataset and dataloader
        ds_kwargs = {
            'custom_class_args': {
                'S': S},
            'custom_class': TruncatedNormal,
            'transform_train': None,
            'transform_test': None,
            'label_mapping': None}
        ds = DataSet('truncated_normal', TRUNCATED_MULTIVARIATE_NORMAL_REQUIRED_ARGS,
                     TRUNCATED_MULTIVARIATE_NORMAL_OPTIONAL_ARGS, data_path=None, **ds_kwargs)
        loaders = ds.make_loaders(workers=config.args.workers, batch_size=config.args.batch_size)
        # initialize model with empiricial estimates
        self._normal = MultivariateNormal(loaders[0].dataset.loc, loaders[0].dataset.var.unsqueeze(0))
        # keep track of gradients for mean and covariance matrix
        self._normal.loc.requires_grad, self._normal.covariance_matrix.requires_grad = True, True
        # initialize projection set and add iteration hook to hyperparameters
        self.projection_set = TruncatedNormalProjectionSet(self._normal.loc, self._normal.covariance_matrix)
        config.args.__setattr__('iteration_hook', self.projection_set)
        # exponent class
        self.exp_h = Exp_h(self._normal.loc, self._normal.covariance_matrix)
        config.args.__setattr__('exp_h', self.exp_h)
        # run PGD to predict actual estimates
        return train_model(config.args, self._normal, loaders,
                           update_params=[self._normal.loc, self._normal.covariance_matrix])


class TruncatedNormalProjectionSet:
    """
    Truncated normal distribution with unknown truncation projection set.
    """

    def __init__(self, emp_loc, emp_scale):
        """
        Args:
            emp_loc (torch.Tensor): empirical mean
            emp_scale (torch.Tensor): empirical variance
        """
        # projection set parameters
        self.emp_loc = emp_loc
        self.emp_scale = emp_scale
        self.radius = config.args.radius * ch.sqrt(ch.log(1.0 / config.args.alpha))

        # upper and lower bounds
        if config.args.clamp:
            self.loc_bounds, self.scale_bounds = Bounds(self.emp_loc - self.radius, self.emp_loc + self.radius), \
                                                 Bounds(ch.max(config.args.alpha.pow(2) / 12,
                                                               self.emp_scale - self.radius),
                                                        self.emp_scale + self.radius)
        else:
            pass

    def __call__(self, M, i, loop_type, inp, target):
        if config.args.clamp:
            M.loc.data = ch.clamp(M.loc.data, float(self.loc_bounds.lower), float(self.loc_bounds.upper))
            M.covariance_matrix.data = ch.clamp(M.covariance_matrix.data, float(self.scale_bounds.lower),
                                                float(self.scale_bounds.upper))
        else:
            pass


# HELPER FUNCTIONS
class Exp_h:
    def __init__(self, emp_loc, emp_cov):
        self.emp_loc = emp_loc
        self.emp_cov = emp_cov
        self.pi_const = (self.emp_loc.size(0) / 2.0) * ch.log(2.0 * Tensor([ch.acos(ch.zeros(1)).item() * 2]).unsqueeze(0))

    def __call__(self, u, B, x):
        """
        returns: evaluates exponential function
        """
        cov_term = ch.bmm(x.unsqueeze(1).matmul(B), x.unsqueeze(2)).flatten(1) / 2.0
        trace_term = ch.trace((B - ch.eye(u.size(0))) * (self.emp_cov + self.emp_loc.matmul(self.emp_loc))).unsqueeze(0)
        loc_term = (x - self.emp_loc).matmul(u.unsqueeze(1))
        return ch.exp(cov_term - trace_term - loc_term + self.pi_const)