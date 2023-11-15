# coding: utf-8
"""
======================================================
Paddle Backend Example: Model Fusion by Graph Matching
======================================================

This example shows how to fuse different models into a single model by ``pygmtools``.
Model fusion aims to fuse multiple models into one, such that the fused model could have higher performance.
The neural networks can be regarded as graphs (channels - nodes, update functions between channels - edges;
node feature - bias, edge feature - weights), and fusing the models is equivalent to solving a graph matching
problem. In this example, the given models are trained on MNIST data from different distributions, and the
fused model could combine the knowledge from two input models and can reach higher accuracy when testing.
"""

# Author: Chang Liu <only-changer@sjtu.edu.cn>
#         Runzhong Wang <runzhong.wang@sjtu.edu.cn>
#         Wenzheng Pan <pwz1121@sjtu.edu.cn>
#
# License: Mulan PSL v2 License
# sphinx_gallery_thumbnail_number = 1

##############################################################################
# .. note::
#     This is a simplified implementation of the ideas in `Liu et al. Deep Neural Network Fusion via Graph Matching with Applications to Model Ensemble and Federated Learning. ICML 2022. <https://proceedings.mlr.press/v162/liu22k/liu22k.pdf>`_
#     For more details, please refer to the paper and the `official code repository <https://github.com/Thinklab-SJTU/GAMF>`_.
#
# .. note::
#     The following solvers are included in this example:
#
#     * :func:`~pygmtools.classic_solvers.sm` (classic solver)
#
#     * :func:`~pygmtools.linear_solvers.hungarian` (linear solver)
#

import paddle
import paddle.nn as nn
import paddle.nn.functional as F
import paddle.vision.transforms as transforms
import time
from PIL import Image
import matplotlib.pyplot as plt
import pygmtools as pygm
import warnings
warnings.filterwarnings("ignore")

pygm.set_backend('paddle')
device = paddle.device.get_device()
paddle.device.set_device(device)

##############################################################################
# Define a simple CNN classifier network
# ---------------------------------------
#
class SimpleNet(nn.Layer):
    def __init__(self):
        super(SimpleNet, self).__init__()
        self.conv1 = nn.Conv2D(1, 32, 5, padding=1, padding_mode='replicate', bias_attr=False)
        self.max_pool = nn.MaxPool2D(2, padding=1)
        self.conv2 = nn.Conv2D(32, 64, 5, padding=1, padding_mode='replicate', bias_attr=False)
        self.fc1 = nn.Linear(3136, 32, bias_attr=False)
        self.fc2 = nn.Linear(32, 10, bias_attr=False)

    def forward(self, x):
        output = F.relu(self.conv1(x))
        output = self.max_pool(output)
        output = F.relu(self.conv2(output))
        output = self.max_pool(output)
        output = output.reshape((output.shape[0], -1))
        output = self.fc1(output)
        output = self.fc2(output)
        return output


##############################################################################
# Load the trained models to be fused
# ------------------------------------
#
model1 = SimpleNet()
model2 = SimpleNet()
model1.set_dict(paddle.load('../data/example_model_fusion_1_paddle.dat'))
model2.set_dict(paddle.load('../data/example_model_fusion_2_paddle.dat'))
model1.to(device)
model2.to(device)
test_dataset = paddle.vision.datasets.MNIST(
    # unable to modify the directory to store the dataset.
    # default: ~/.cache/paddle/dataset/mnist
    mode='test',  # the dataset is used to test
    transform=transforms.ToTensor(),  # the dataset is in the form of tensors
    download=True)
test_loader = paddle.io.DataLoader(
    dataset=test_dataset,
    batch_size=32,
    shuffle=False)

##############################################################################
# Print the layers of the simple CNN model:
#
print(model1)

##############################################################################
# Test the input models
# ------------------------------------
#
with paddle.no_grad():
    n_correct1 = 0
    n_correct2 = 0
    n_samples = 0
    for images, labels in test_loader:
        outputs1 = model1(images)
        outputs2 = model2(images)
        predictions1 = paddle.argmax(outputs1, 1)
        predictions2 = paddle.argmax(outputs2, 1)
        n_samples += labels.shape[0]
        n_correct1 += (predictions1 == labels.t()).sum().item()
        n_correct2 += (predictions2 == labels.t()).sum().item()
    acc1 = 100 * n_correct1 / n_samples
    acc2 = 100 * n_correct2 / n_samples

##############################################################################
# Testing results (two separate models):
#
print(f'model1 accuracy = {acc1}%, model2 accuracy = {acc2}%')

##############################################################################
# Build the affinity matrix for graph matching
# ---------------------------------------------
# As shown in the following plot, the neural networks can be regarded as graphs. The weights correspond to
# the edge features, and the bias corresponds to the node features. In this example, the neural network
# does not have bias so that there are only edge features.
#
plt.figure(figsize=(8, 4))
img = Image.open('../data/model_fusion.png')
plt.imshow(img)
plt.axis('off')
st_time = time.perf_counter()


##############################################################################
# Define the graph matching affinity metric function
#
class Ground_Metric_GM:
    def __init__(self,
                 model_1_param: paddle.Tensor = None,
                 model_2_param: paddle.Tensor = None,
                 conv_param: bool = False,
                 bias_param: bool = False,
                 pre_conv_param: bool = False,
                 pre_conv_image_size_squared: int = None):
        self.model_1_param = model_1_param
        self.model_2_param = model_2_param
        self.conv_param = conv_param
        self.bias_param = bias_param
        # bias, or fully-connected from linear
        if bias_param is True or (conv_param is False and pre_conv_param is False):
            self.model_1_param = self.model_1_param.reshape((1, -1, 1))
            self.model_2_param = self.model_2_param.reshape((1, -1, 1))
        # fully-connected from conv
        elif conv_param is False and pre_conv_param is True:
            self.model_1_param = self.model_1_param.reshape((1, -1, pre_conv_image_size_squared))
            self.model_2_param = self.model_2_param.reshape((1, -1, pre_conv_image_size_squared))
        # conv
        else:
            self.model_1_param = self.model_1_param.reshape((1, -1, model_1_param.shape[-1]))
            self.model_2_param = self.model_2_param.reshape((1, -1, model_2_param.shape[-1]))

    def process_distance(self, p: int = 2):
        dist = []
        cdist = paddle.nn.PairwiseDistance(p)
        param_1 = self.model_1_param.cast('float32')[0]
        param_2 = self.model_2_param.cast('float32')[0]
        for i in param_1:
            dist.append(cdist(i.broadcast_to(param_2.shape), param_2))
        return paddle.to_tensor(dist)

    def process_soft_affinity(self, p: int = 2):
        return paddle.exp(0 - self.process_distance(p=p))


##############################################################################
# Define the affinity function between two neural networks. This function takes multiple neural network modules,
# and construct the corresponding affinity matrix which is further processed by the graph matching solver.
#
def graph_matching_fusion(networks: list):
    def total_node_num(network: paddle.nn.Layer):
        # count the total number of nodes in the network [network]
        num_nodes = 0
        for idx, (name, parameters) in enumerate(network.named_parameters()):
            if 'bias' in name:
                continue
            if idx == 0:
                num_nodes += parameters.shape[1]
            # transpose linear layers in paddle to conventional shape,
            num_nodes += parameters.shape[0] if 'fc' not in name else parameters.shape[1] 
        return num_nodes

    n1 = total_node_num(network=networks[0])
    n2 = total_node_num(network=networks[1])
    assert (n1 == n2)
    affinity = paddle.zeros([n1 * n2, n1 * n2])
    num_layers = len(list(zip(networks[0].parameters(), networks[1].parameters())))
    num_nodes_before = 0
    num_nodes_incremental = []
    num_nodes_layers = []
    pre_conv_list = []
    cur_conv_list = []
    conv_kernel_size_list = []
    num_nodes_pre = 0
    is_conv = False
    pre_conv = False
    pre_conv_out_channel = 1
    is_final_bias = False
    perm_is_complete = True
    named_weight_list_0 = [named_parameter for named_parameter in networks[0].named_parameters()]
    for idx, ((name_0, fc_layer0_weight), (name_1, fc_layer1_weight)) in \
            enumerate(zip(networks[0].named_parameters(), networks[1].named_parameters())):
        assert fc_layer0_weight.shape == fc_layer1_weight.shape
        if 'fc' in name_0:
            fc_layer0_weight = fc_layer0_weight.t()
            fc_layer1_weight = fc_layer1_weight.t()
        layer_shape = fc_layer0_weight.shape
        num_nodes_cur = fc_layer0_weight.shape[0]
        if len(layer_shape) > 1:
            if is_conv is True and len(layer_shape) == 2:
                num_nodes_pre = pre_conv_out_channel
            else:
                num_nodes_pre = fc_layer0_weight.shape[1]
        if idx >= 1 and len(named_weight_list_0[idx - 1][1].shape) == 1:
            pre_bias = True
        else:
            pre_bias = False
        if len(layer_shape) > 2:
            is_bias = False
            if not pre_bias:
                pre_conv = is_conv
                pre_conv_list.append(pre_conv)
            is_conv = True
            cur_conv_list.append(is_conv)
            fc_layer0_weight_data = fc_layer0_weight.detach().reshape(
                (fc_layer0_weight.shape[0], fc_layer0_weight.shape[1], -1))
            fc_layer1_weight_data = fc_layer1_weight.detach().reshape(
                (fc_layer1_weight.shape[0], fc_layer1_weight.shape[1], -1))
        elif len(layer_shape) == 2:
            is_bias = False
            if not pre_bias:
                pre_conv = is_conv
                pre_conv_list.append(pre_conv)
            is_conv = False
            cur_conv_list.append(is_conv)
            fc_layer0_weight_data = fc_layer0_weight.detach()
            fc_layer1_weight_data = fc_layer1_weight.detach()
        else:
            is_bias = True
            if not pre_bias:
                pre_conv = is_conv
                pre_conv_list.append(pre_conv)
            is_conv = False
            cur_conv_list.append(is_conv)
            fc_layer0_weight_data = fc_layer0_weight.detach()
            fc_layer1_weight_data = fc_layer1_weight.detach()
        if is_conv:
            pre_conv_out_channel = num_nodes_cur
        if is_bias is True and idx == num_layers - 1:
            is_final_bias = True
        if idx == 0:
            for a in range(num_nodes_pre):
                affinity[(num_nodes_before + a) * n2 + num_nodes_before + a, \
                         (num_nodes_before + a) * n2 + num_nodes_before + a] \
                        = 1
        if idx == num_layers - 2 and 'bias' in named_weight_list_0[idx + 1][0] or \
                idx == num_layers - 1 and 'bias' not in named_weight_list_0[idx][0]:
            for a in range(num_nodes_cur):
                affinity[(num_nodes_before + num_nodes_pre + a) * n2 + num_nodes_before + num_nodes_pre + a, \
                         (num_nodes_before + num_nodes_pre + a) * n2 + num_nodes_before + num_nodes_pre + a] \
                        = 1
        if is_bias is False:
            ground_metric = Ground_Metric_GM(
                fc_layer0_weight_data, fc_layer1_weight_data, is_conv, is_bias,
                pre_conv, int(fc_layer0_weight_data.shape[1] / pre_conv_out_channel))
        else:
            ground_metric = Ground_Metric_GM(
                fc_layer0_weight_data, fc_layer1_weight_data, is_conv, is_bias,
                pre_conv, 1)

        layer_affinity = ground_metric.process_soft_affinity(p=2)

        if is_bias is False:
            pre_conv_kernel_size = fc_layer0_weight.shape[3] if is_conv else None
            conv_kernel_size_list.append(pre_conv_kernel_size)
        if is_bias is True and is_final_bias is False:
            for a in range(num_nodes_cur):
                for c in range(num_nodes_cur):
                    affinity[(num_nodes_before + a) * n2 + num_nodes_before + c, \
                             (num_nodes_before + a) * n2 + num_nodes_before + c] \
                            = layer_affinity[a][c]
        elif is_final_bias is False:
            for a in range(num_nodes_pre):
                for b in range(num_nodes_cur):
                    affinity[
                    (num_nodes_before + a) * n2 + num_nodes_before:
                    (num_nodes_before + a) * n2 + num_nodes_before + num_nodes_pre,
                    (num_nodes_before + num_nodes_pre + b) * n2 + num_nodes_before + num_nodes_pre:
                    (num_nodes_before + num_nodes_pre + b) * n2 + num_nodes_before + num_nodes_pre + num_nodes_cur] \
                        = layer_affinity[a + b * num_nodes_pre].reshape((num_nodes_cur, num_nodes_pre)).t()
        if is_bias is False:
            num_nodes_before += num_nodes_pre
            num_nodes_incremental.append(num_nodes_before)
            num_nodes_layers.append(num_nodes_cur)
    # affinity = (affinity + affinity.t()) / 2
    return affinity, [n1, n2, num_nodes_incremental, num_nodes_layers, cur_conv_list, conv_kernel_size_list]


##############################################################################
# Get the affinity (similarity) matrix between model1 and model2.
#
K, params = graph_matching_fusion([model1, model2])

##############################################################################
# Align the models by graph matching
# -----------------------------------
# Align the channels of model1 & model2 by maximize the affinity (similarity) via graph matching algorithms.
#
n1 = params[0]
n2 = params[1]
X = pygm.sm(K, n1, n2)

##############################################################################
# Project ``X`` to neural network matching result. The neural network matching matrix is built by applying
# Hungarian to small blocks of ``X``, because only the channels from the same neural network layer can be
# matched.
#
# .. note::
#     In this example, we assume the last FC layer is aligned and need not be matched.
#
new_X = paddle.zeros_like(X)
new_X[:params[2][0], :params[2][0]] = paddle.eye(params[2][0])
for start_idx, length in zip(params[2][:-1], params[3][:-1]):  # params[2] and params[3] are the indices of layers
    slicing = slice(start_idx, start_idx + length)
    new_X[slicing, slicing] = pygm.hungarian(X[slicing, slicing])
# assume the last FC layer is aligned
slicing = slice(params[2][-1], params[2][-1] + params[3][-1])
new_X[slicing, slicing] = paddle.eye(params[3][-1])
X = new_X

##############################################################################
# Visualization of the matching result. The black lines splits the channels of different layers.
#
plt.figure(figsize=(4, 4))
plt.imshow(X.cpu().numpy(), cmap='Blues')
for idx in params[2]:
    plt.axvline(x=idx, color='k')
    plt.axhline(y=idx, color='k')


##############################################################################
# Define the alignment function: fuse the models based on matching result
#
def align(solution, fusion_proportion, networks: list, params: list):
    [_, _, num_nodes_incremental, num_nodes_layers, cur_conv_list, conv_kernel_size_list] = params
    named_weight_list_0 = [named_parameter for named_parameter in networks[0].named_parameters()]
    aligned_wt_0 = [parameter.detach() if 'fc' not in name else parameter.detach().t() for name, parameter in named_weight_list_0]
    idx = 0
    num_layers = len(aligned_wt_0)
    for num_before, num_cur, cur_conv, cur_kernel_size in \
            zip(num_nodes_incremental, num_nodes_layers, cur_conv_list, conv_kernel_size_list):
        perm = solution[num_before:num_before + num_cur, num_before:num_before + num_cur]
        assert 'bias' not in named_weight_list_0[idx][0]
        if len(named_weight_list_0[idx][1].shape) == 4:
            aligned_wt_0[idx] = (perm.t().cast(paddle.float64) @
                                 aligned_wt_0[idx].cast(paddle.float64).transpose((2, 3, 0, 1))) \
                .transpose((2, 3, 0, 1))
        else:
            aligned_wt_0[idx] = perm.t().cast(paddle.float64) @ aligned_wt_0[idx].cast(paddle.float64)
        idx += 1
        if idx >= num_layers:
            continue
        if 'bias' in named_weight_list_0[idx][0]:
            aligned_wt_0[idx] = aligned_wt_0[idx].cast(paddle.float64) @ perm.cast(paddle.float64)
            idx += 1
        if idx >= num_layers:
            continue
        if cur_conv and len(named_weight_list_0[idx][1].shape) == 2:
            aligned_wt_0[idx] = (aligned_wt_0[idx].cast(paddle.float64)
                                 .reshape((aligned_wt_0[idx].shape[0], 64, -1))
                                 .transpose((0, 2, 1))
                                 @ perm.cast(paddle.float64)) \
                .transpose((0, 2, 1)) \
                .reshape((aligned_wt_0[idx].shape[0], -1))
        elif len(named_weight_list_0[idx][1].shape) == 4:
            aligned_wt_0[idx] = (aligned_wt_0[idx].cast(paddle.float64)
                                 .transpose((2, 3, 0, 1))
                                 @ perm.cast(paddle.float64)) \
                .transpose((2, 3, 0, 1))
        else:
            aligned_wt_0[idx] = aligned_wt_0[idx].cast(paddle.float64) @ perm.cast(paddle.float64)
    assert idx == num_layers

    averaged_weights = []
    for idx, (named, parameter) in enumerate(networks[1].named_parameters()):
        parameter = parameter.t() if 'fc' in named else parameter          
        averaged_weights.append((1 - fusion_proportion) * aligned_wt_0[idx].cast('float32') + fusion_proportion * parameter)
    return averaged_weights


##############################################################################
# Test the fused model
# ---------------------
# The ``fusion_proportion`` variable denotes the contribution to the new model. For example, if ``fusion_proportion=0.2``,
# the fused model = 80% model1 + 20% model2.
#
def align_model_and_test(X):
    acc_list = []
    for fusion_proportion in paddle.arange(0, 11, 1) / 10: # paddle arange accepts int step only
        fused_weights = align(X, fusion_proportion, [model1, model2], params)

        fused_model = SimpleNet()
        state_dict = fused_model.state_dict()
        for idx, (key, _) in enumerate(state_dict.items()):
            state_dict[key] = fused_weights[idx].t() if 'fc' in key else fused_weights[idx]
        fused_model.set_dict(state_dict)
        fused_model.to(device)
        test_loss = 0
        correct = 0
        for data, target in test_loader:
            output = fused_model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item()
            pred = output.detach().argmax(1, keepdim=True)
            correct += pred.equal(target.detach().reshape(pred.shape)).sum()
        test_loss /= len(test_loader.dataset)
        acc = 100. * correct / len(test_loader.dataset)
        print(
            f"{1 - fusion_proportion.item():.2f} model1 + {fusion_proportion.item():.2f} model2 -> fused model accuracy: {acc.item():.2f}%")
        acc_list.append(acc)
    return paddle.to_tensor(acc_list)


print('Graph Matching Fusion')
gm_acc_list = align_model_and_test(X)

##############################################################################
# Compare with vanilla model fusion (no matching), graph matching method stabilizes the fusion step:
#
print('No Matching Fusion')
vanilla_acc_list = align_model_and_test(paddle.eye(n1))

plt.figure(figsize=(4, 4))
plt.title('Fused Model Accuracy')
plt.plot((paddle.arange(0, 11, 1) / 10).numpy(), gm_acc_list.cpu().numpy(), 'r*-', label='Graph Matching Fusion')
plt.plot((paddle.arange(0, 11, 1) / 10).numpy(), vanilla_acc_list.cpu().numpy(), 'b*-', label='No Matching Fusion')
plt.plot((paddle.arange(0, 11, 1) / 10).numpy(), [acc1] * 11, '--', color="gray", label='Model1 Accuracy')
plt.plot((paddle.arange(0, 11, 1) / 10).numpy(), [acc2] * 11, '--', color="brown", label='Model2 Accuracy')
plt.gca().set_xlabel('Fusion Proportion')
plt.gca().set_ylabel('Accuracy (%)')
plt.ylim((70, 87))
plt.legend(loc=3)

##############################################################################
# Print the result summary
# ------------------------------------
#
end_time = time.perf_counter()
print(f'time consumed for model fusion: {end_time - st_time:.2f} seconds')
print(f'model1 accuracy = {acc1}%, model2 accuracy = {acc2}%')
print(f"best fused model accuracy: {(paddle.max(gm_acc_list)).item():.2f}%")

##############################################################################
# .. note::
#     This example supports both GPU and CPU, and the online documentation is built by a CPU-only machine.
#     The efficiency will be significantly improved if you run this code on GPU.
#
