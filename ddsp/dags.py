# Copyright 2021 The DDSP Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Library of functions and layers of Directed Acyclical Graphs.

DAGLayer exists as an alternative to manually specifying the forward pass in
python. The advantage is that a variety of configurations can be
programmatically specified via external dependency injection, such as with the
`gin` library.
"""

from typing import Dict, Sequence, Tuple, Text, TypeVar

from absl import logging
from ddsp import core
import gin
import tensorflow.compat.v2 as tf

tfkl = tf.keras.layers

# Define Types.
TensorDict = Dict[Text, tf.Tensor]
KeyOrModule = TypeVar('KeyOrModule', Text, tf.Module)
Node = Tuple[KeyOrModule, Sequence[Text], Sequence[Text]]
DAG = Sequence[Node]

# Helper Functions for DAGs  ---------------------------------------------------
filter_by_value = lambda d, cond: dict(filter(lambda e: cond(e[1]), d.items()))
is_module = lambda v: isinstance(v, tf.Module)

# Duck typing.
is_loss = lambda v: hasattr(v, 'get_losses_dict')
is_processor = lambda v: hasattr(v, 'get_signal') and hasattr(v, 'get_controls')


def split_keras_kwargs(kwargs):
  """Strip keras specific kwargs."""
  keras_kwargs = {}
  for key in ['training', 'mask', 'name']:
    if kwargs.get(key) is not None:
      keras_kwargs[key] = kwargs.pop(key)
  return keras_kwargs, kwargs


# DAG and ProcessorGroup Classes -----------------------------------------------
@gin.register
class DAGLayer(tfkl.Layer):
  """String modules together."""

  def __init__(self, dag: DAG, **kwarg_modules):
    """Constructor.

    Args:
      dag: A directed acyclical graph in the form of a list of nodes. Each node
        has the form

        ['module', ['input_key', ...], ['output_key', ...]]

        'module': Module instance or string name of module. For example,
          'encoder' woud access the attribute `dag_layer.encoder`.
        'input_key': List of strings, nested keys in dictionary of dag outputs.
          For example, 'inputs/f0_hz' would access `outputs[inputs]['f0_hz']`.
          Inputs to the dag are wrapped in a `inputs` dict as shown in the
          example. This list is ordered and has one key per a module input
          argument. Each node's outputs are prefixed by their module name.
        'output_key': List of strings, keys for each return value of the module.
          For example, ['amps', 'freqs'] would have the module return a dict
          {'module_name': {'amps': return_value_0, 'freqs': return_value_1}}.
          If the module returns a dictionary, the keys of the dictionary will be
          used and these values (if provided) will be ignored.

        The graph is read sequentially and must be topologically sorted. This
        means that all inputs for a module must already be generated by earlier
        modules (or in the input dictionary).
      **kwarg_modules: A series of modules to add to DAGLayer. Each kwarg that
        is a tf.Module will be added as a property of the layer, so that it will
        be accessible as `dag_layer.kwarg`. Also, other keras kwargs such as
        'name' are split off before adding modules.
    """
    keras_kwargs, kwarg_modules = split_keras_kwargs(kwarg_modules)
    super().__init__(**keras_kwargs)

    # Create properties/submodules from other kwargs.
    modules = filter_by_value(kwarg_modules, is_module)

    # Remove modules from the dag, make properties of dag_layer.
    dag, dag_modules = self.format_dag(dag)
    # DAG is now just strings.
    self.dag = dag
    modules.update(dag_modules)

    # Make as propreties of DAGLayer to keep track of variables in checkpoints.
    self.module_names = list(modules.keys())
    for module_name, module in modules.items():
      setattr(self, module_name, module)

  @property
  def modules(self):
    """Module getter."""
    return [getattr(self, name) for name in self.module_names]

  @staticmethod
  def format_dag(dag):
    """Remove modules from dag, and replace with module names."""
    modules = {}
    dag = list(dag)  # Make mutable in case it's a tuple.
    for i, node in enumerate(dag):
      node = list(node)  # Make mutable in case it's a tuple.
      module = node[0]
      if is_module(module):
        # Strip module from the dag.
        modules[module.name] = module
        # Replace with module name.
        node[0] = module.name
      dag[i] = node
    return dag, modules

  def call(self, inputs: TensorDict, **kwargs) -> tf.Tensor:
    """Run dag for an input dictionary."""
    return self.run_dag(inputs, **kwargs)

  @gin.configurable(allowlist=['verbose'])  # For debugging.
  def run_dag(self,
              inputs: TensorDict,
              verbose: bool = True,
              **kwargs) -> TensorDict:
    """Connects and runs submodules of dag.

    Args:
      inputs: A dictionary of input tensors fed to the dag.
      verbose: Print out dag routing when running.
      **kwargs: Other kwargs to pass to submodules, such as keras kwargs.

    Returns:
      A nested dictionary of all the output tensors.
    """
    # Initialize the outputs with inputs to the dag.
    outputs = {'inputs': inputs}
    # TODO(jesseengel): Remove this cluttering of the base namespace. Only there
    # for backwards compatability.
    outputs.update(inputs)

    # Run through the DAG nodes in sequential order.
    for node in self.dag:
      # The first element of the node can be either a module or module_key.
      module_key, input_keys = node[0], node[1]
      module = getattr(self, module_key)
      # Optionally specify output keys if module does not return dict.
      output_keys = node[2] if len(node) > 2 else None

      # Get the inputs to the node.
      inputs = [core.nested_lookup(key, outputs) for key in input_keys]

      if verbose:
        shape = lambda d: tf.nest.map_structure(lambda x: list(x.shape), d)
        logging.info('Input to Module: %s\nKeys: %s\nIn: %s\n',
                     module_key, input_keys, shape(inputs))

      # Duck typing to avoid dealing with multiple inheritance of Group modules.
      if is_processor(module):
        # Processor modules.
        module_outputs = module(*inputs, return_outputs_dict=True, **kwargs)
      elif is_loss(module):
        # Loss modules.
        module_outputs = module.get_losses_dict(*inputs, **kwargs)
      else:
        # Network modules.
        module_outputs = module(*inputs, **kwargs)

      if not isinstance(module_outputs, dict):
        module_outputs = core.to_dict(module_outputs, output_keys)

      if verbose:
        logging.info('Output from Module: %s\nOut: %s\n',
                     module_key, shape(module_outputs))

      # Add module outputs to the dictionary.
      outputs[module_key] = module_outputs

    # Alias final module output as dag output.
    # 'out' is a reserved key for final dag output.
    outputs['out'] = module_outputs

    return outputs
