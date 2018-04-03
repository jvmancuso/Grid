import torch
from ..base import BaseService
from ... import channels
from ...lib import utils, torch_utils as tu

from collections import OrderedDict
from functools import wraps, partial, partialmethod
import inspect
import random
import re
from types import *


class HookService(BaseService):
    def __init__(self, worker):
        super().__init__(worker)

        # Methods that caused infinite recursion during testing
        # TODO: May want to handle the ones in "exclude" manually at
        #       some point
        self.exclude = (['ndimension', 'nelement', 'size', 'numel',
            'type', 'tolist', 'dim', '__iter__', 'select'])
        # This one wasn't in dir(Variable) -- probably a C++ thing
        self.var_exclude = ['__getattr__']
        # Torch functions we don't want to override
        self.torch_exclude = ['save', 'load', 'typename']

        # Perform overloading
        print('Hooking into Torch...')
        self.hook_torch_module()
        for t_type in self.tensor_types:
            self.hook_tensor(t_type)
        self.hook_variable()
        print('Overloading complete.')


    ## Registration and communication handlers
    def send_obj(self, obj, recipient):
        """Send Torch object to recipient."""
        self.worker.publish(
            channels.torch_listen_for_obj_callback(recipient),
            message=obj.ser(include_data=True))


    def request_obj(self, obj, sender):
        """Request Torch object from sender."""
        return self.worker.request_response(
            channel=channels.torch_listen_for_obj_req_callback(sender),
            message=obj.id,
            response_handler=self.worker.services['torch_service'].receive_obj_break)


    def send_command(self, command, recipient):
        """Send Torch command to recipient."""
        response = self.worker.request_response(
            channels.torch_listen_for_command_callback(recipient),
            message=command,
            response_handler=self.process_response)
        return response


    def assemble_result_pointer(self, registration, torch_type):
        """
        Assembles a pointer to a remote Torch object. Pointers feel like
        real Torch objects, but they're zero-dimensional until their
        contents are retrieved from their owners.

        Args
        registration (dict): registration attributes for the pointer
        torch_type: the torch class to construct the pointer from
        """
        # TODO: extend to iterables of tensor pointers
        try:
            torch_type = tu.types_guard(torch_type)
        except KeyError:
            raise TypeError(
                "Tried to receive a non-Torch object of type {}.".format(
                    _tensor_type))
        result = torch_type(0)
        return self.register_object(result, **registration)


    def process_response(self, response):
        """Processes a worker's response from a command."""
        # TODO: Extend to responses that are iterables.
        response = utils.unpack(response)
        try:
            return response['numeric']
        except KeyError:
            return response


    @staticmethod
    def compile_command(partial_func, has_self):
        """
        Assembles a JSON-serializable message from a partial function.

        Args:
        partial_func: a functools.partial or functools.partialmethod
            object wrapped around a torch command, its args, and its
            kwargs.
        has_self: a flag for whether or not the function is a method.
        """
        func = partial_func.func
        args = partial_func.args
        kwargs = partial_func.keywords
        command = {}
        command['has_self'] = has_self
        if has_self:
            command['self'] = args[0]
            args = args[1:]
        command['command'] = func.__name__
        command['args'] = args
        command['kwargs'] = kwargs
        command['arg_types'] = [type(x).__name__ for x in args]
        command['kwarg_types'] = [type(kwargs[x]).__name__ for x in kwargs]
        return command


    ## Grid-specific method hooking
    def hook_tensor_send(service_self, tensor_type):
        def send_(self, workers):
            """
            Sends a Tensor object to a (sequence of) Grid workers.

            Args:
            workers: string (or sequence) containing IPFS address(es)
                of worker node(s).
            """
            workers = tu.check_workers(self, workers) # makes singleton, if needed
            self = service_self.register_object(self, id=self.id, owners=workers)
            for worker in workers:
                # TODO: sync or async? likely won't be worth doing async,
                #       but should check (low priority)
                service_self.send_obj(self, worker)
            self = service_self.register_object(self.old_set_(tensor_type(0)),
                id=self.id, owners=workers, is_pointer=True)
            return self

        setattr(tensor_type, 'send_', send_)


    def hook_tensor_get(service_self, tensor_type):
        def get_(self, reduce=lambda x:x[0]):
            """
            Gets a Tensor object from its current owners.

            Args:
            reduce: (EXPERIMENTAL) How to reduce tensors that come from
                multiple workers
            """
            # TODO: fully generalize this to multiple workers; consider
            #       adding arguments for other tensor ids, e.g. mapping workers
            #       to tensors, and a reduce function (for example, would allow
            #       for built-in gradient averaging when Variable.get is done)
            #       (low priority)
            if service_self.worker.id in self.owners:
                return self
            collected = []
            for worker in self.owners:
                x = service_self.request_obj(self, worker)
                collected.append(service_self.register_object(x, id=x.id))  
            return service_self.register_object(self.old_set_(reduce(collected)), id=self.id)
        setattr(tensor_type, 'get_', get_)


    # TODO: Variable.send, Variable.get (will need to send/get Variable
    #       registration attributes, handling data and grad tensors properly)
    #       Resolve Issue #148 before attempting


    ## General hooking wrappers
    @staticmethod
    def pass_func_args(func):
        """Wrapper gathering partial object from function call."""
        @wraps(func)
        def pass_args(*args, **kwargs):
            return partial(func, *args, **kwargs)
        return pass_args


    def overload_function(self, func):
        """
        Wrapper overloading partial objects of functions in the torch
        module.  Compiles command, checks for Tensors and Variables in
        the args/kwargs, determines locations of all Tensors and
        Variables involved in computation, and handles the computation
        accordingly.
        """
        @wraps(func)
        def command_workers(*args, **kwargs):
            part = func(*args, **kwargs)
            command = self.compile_command(part, has_self = False)
            tensorvars = tu.get_tensorvars(self, command)
            has_remote = tu.check_remote(tensorvars)
            if has_remote:
                multiple_owners, owners = tu.get_owners(tensorvars)
                if multiple_owners:
                    raise NotImplementedError("""MPC not yet implemented: 
                    Torch objects need to be on the same machine in order
                    to compute with them.""")
                else:
                    command = tu.replace_in_command(command)
                    for worker in owners:
                        # only returns last pointer, since tensors will
                        # be identical across machines for right now.
                        # if response is numeric, returns first owner's
                        # result
                        # TODO: extend to iterables of pointers
                        response = self.send_command(command, worker)
                        try:
                            pointer = self.assemble_result_pointer(
                                response['registration'],
                                response['torch_type'])
                        except KeyError:
                            return response
                    return pointer
            else:
                result = part.func(*args, **kwargs)
                if type(result) in self.tensorvar_types:
                    result = self.register_object(result, is_pointer=False)
                return result
                
        return command_workers


    @staticmethod
    def pass_method_args(method):
        """Wrapper gathering partialmethod object from method call."""
        @wraps(method)
        def pass_args(*args, **kwargs):
            return partialmethod(method, *args, **kwargs)
        return pass_args


    def overload_method(service_self, method):
        """
        Wrapper overloading partialmethod objects of Torch object
        methods.  Compiles command, checks for Tensors and Variables in
        the args/kwargs, determines locations of all Tensors and
        Variables involved in computation, and handles the computation
        accordingly.
        """
        @wraps(method)
        def command_workers(self, *args, **kwargs):
            part = method(self, *args, **kwargs)
            if self.is_pointer:
                command = service_self.compile_command(part, has_self=True)
                tensorvars = tu.get_tensorvars(service_self, command)
                has_remote = tu.check_remote(tensorvars)
                multiple_owners, owners = tu.get_owners(tensorvars)
                if has_remote and not multiple_owners:
                    for worker in owners:
                        # only returns last pointer, since tensors will
                        # be identical across machines for right now.
                        # if response is numeric, returns first owner's
                        # result
                        # TODO: extend to iterables of pointers
                        command = tu.replace_in_command(command)
                        response = self.send_command(command, worker)
                        try:
                            pointer = self.assemble_result_pointer(
                                response['registration'],
                                response['torch_type'])
                        except KeyError:
                            return response
                else:
                    raise NotImplementedError("""MPC not yet implemented:
                        Torch objects need to be on the same machine in
                        order to compute with them.""")
                return pointer
            else:
                result = part.func(self, *args, **kwargs)
                if (type(result) in service_self.tensorvar_types and 
                    not hasattr(result, 'owner')):
                    result = service_self.register_object(result,
                        is_pointer=False)
                return result
        return command_workers


    ## Special Tensor method hooks
    def hook_tensor___init__(service_self, tensor_type):
        """Overload tensor_type.__init__"""
        def new___init__(self, *args):
            super(tensor_type, self).__init__()
            self = service_self.register_object(self, is_pointer=False)

        tensor_type.__init__ = new___init__
    

    def hook_tensor___new__(service_self, tensor_type):
        """Overload tensor_type.__new__"""
        tensor_type.old___new__ = tensor_type.__new__
        def new___new__(cls, *args, **kwargs):
            result = cls.old___new__(cls, *args,  **kwargs)
            result = service_self.register_object(result, is_pointer=False)
            return result
        
        tensor_type.__new__ = new___new__


    def hook_tensor___repr__(service_self, tensor_type):
        """Overload tensor_type.__repr__"""
        tensor_type.old__repr__ = tensor_type.__repr__
        def new___repr__(self):
            if service_self.worker.id in self.owners:
                return self.old__repr__()
            else:
                return "[{}.{} - Locations:{}]".format(
                    tensor_type.__module__,
                    tensor_type.__name__,
                    self.owners)

        tensor_type.__repr__ = new___repr__


    ## Special Variable method hooks
    def hook_var___new__(service_self):
        """Overload Variable.__new__"""
        torch.autograd.variable.Variable.old___new__ = torch.autograd.variable.Variable.__new__
        def new___new__(cls, *args, **kwargs):
            result = cls.old___new__(cls, *args,  **kwargs)
            result = service_self.register_object(result, is_pointer=False)
            return result
        
        torch.autograd.variable.Variable.__new__ = new___new__


    def hook_var_contents(service_self):
        """Overload Variable.data and Variable.grad properties."""
        torch.autograd.variable.Variable.old_data = torch.autograd.variable.Variable.data
        torch.autograd.variable.Variable.old_grad = torch.autograd.variable.Variable.grad
        @property
        def new_data(self):
            try:
                self.data_registered
            except AttributeError:
                self.old_data = service_self.register_object(
                    self.old_data, id=self.id,
                    owners=self.owners, is_pointer=self.is_pointer)
                self.data_registered = True
            return self.old_data
        
        @property
        def new_grad(self):
            try:
                self.grad_registered
            except AttributeError:
                if self.old_grad is not None:
                    self.old_grad = service_self.register_object(
                        self.old_grad, id=self.id,
                    owners=self.owners, is_pointer=self.is_pointer)
                    self.grad_registered = True
            return self.old_grad
        
        torch.autograd.variable.Variable.data = new_data
        torch.autograd.variable.Variable.grad = new_grad


    ## Overloading Torch objects
    def hook_torch_module(self):
        """Overload functions in the main torch module"""
        for attr in self.torch_funcs:

            # Conditions for inclusion/exclusion
            if attr in self.torch_exclude:
                continue

            # Where the overloading happens
            lit = getattr(torch, attr)
            if (type(lit) in [FunctionType, BuiltinFunctionType]):
                passer = self.pass_func_args(lit)
                new_attr = self.overload_function(passer)
                setattr(torch, 'old_{}'.format(attr), lit)
                setattr(torch, attr, new_attr)


    def hook_tensor(self, tensor_type):
        """Overloading a given tensor_type"""
        # Overload 'special' methods here
        self.hook_tensor___init__(tensor_type)
        self.hook_tensor___new__(tensor_type)
        self.hook_tensor___repr__(tensor_type)

        for attr in dir(tensor_type):

            # Conditions for inclusion/exclusion
            if attr in self.exclude:
                continue
            lit = getattr(tensor_type, attr)
            is_base = attr in dir(object)
            is_desc = inspect.ismethoddescriptor(lit)
            is_func = type(lit)==FunctionType
            try:
                is_service_func = 'HookService' in lit.__qualname__
            except:
                is_service_func = False
            is_old = re.match('old*', attr) is not None

            # Where the overloading happens
            if ((is_desc or (is_func and not is_service_func)) 
                and not is_base and not is_old):
                passer = self.pass_method_args(lit)
                new_attr = self.overload_method(passer)
                setattr(tensor_type, 'old_{}'.format(attr), lit)
                setattr(tensor_type, attr, new_attr)

        # Add in our own Grid-specific methods
        self.hook_tensor_send(tensor_type)
        self.hook_tensor_get(tensor_type)
        tu.hook_tensor_ser(self, tensor_type)


    def hook_variable(self):
        # Overload 'special' methods here
        self.hook_var___new__()
        self.hook_var_contents()

        for attr in dir(torch.autograd.variable.Variable):

            # Conditions for inclusion/exclusion
            if attr in self.exclude + self.var_exclude:
                continue
            lit = getattr(torch.autograd.variable.Variable, attr)
            is_base = attr in dir(object)
            is_desc = inspect.ismethoddescriptor(lit)
            is_func = type(lit)==FunctionType
            try:
                is_service_func = 'HookService' in lit.__qualname__
            except:
                is_service_func = False
            is_old = re.match('old*', attr) is not None

            # Where the overloading happens
            if ((is_desc or (is_func and not is_service_func)) 
                and not is_base and not is_old):
                passer = self.pass_method_args(lit)
                new_attr = self.overload_method(passer)
                setattr(torch.autograd.variable.Variable, 
                    'old_{}'.format(attr), lit)
                setattr(torch.autograd.variable.Variable, attr, new_attr)
