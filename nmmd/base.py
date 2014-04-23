'''We're dealing with two types of dispatchers here. One is the
decorator-based dispatcher that can handle args passed into the decorator.
The other does everything by calling user-defined functions to generate
possible names of the dispatchable methods, with no decorator nonsense
required. But ideally there should be an easy migration path from one to
the other if necessary.
'''
import types
import pickle
import inspect
import functools
import collections

from hercules import CachedAttr, CachedClassAttr


class DispatchError(Exception):
    '''Raised when someone does something silly, like
    dispatch two conlicting handlers to process the same
    stream input.
    '''


class DispatchInterrupt(Exception):
    '''Raise to stop dispatcher trying additional dispatch methods.
    '''


class ImplementationError(Exception):
    '''Raise if subclass does stuff wrong.
    '''


class BaseDispatcher(object):
    DispatchError = DispatchError
    DispatchInterrupt = DispatchInterrupt
    GeneratorType = types.GeneratorType

    # Whether to run multiple matching methds or bail after
    # the first (default).
    multi = False

    @CachedAttr
    def dispatch_data(self):
        try:
            return self.prepare()
        except RuntimeError:
            msg ='''
Oh dear. Please don't reference self.dispatch_data inside self.prepare,
because self.dispatch_data references self.prepare. Instead use
self.registry.'''
            raise ImplementationError(msg)

    def prepare(self):
        raise NotImplemented()

    def get_method(self):
        raise NotImplemented()

    def dispatch(self, *args, **kwargs):
        raise NotImplemented()


class Dispatcher(BaseDispatcher):
    '''Implements the base functionality for dispatcher types.
    The node instances delegate their dispatch functions to
    subclasses of Dispatcher.
    '''
    __slots__ = tuple()

    def __call__(self, *args, **kwargs):
        return self._make_decorator(*args, **kwargs)

    def __get__(self, inst, cls=None):
        self.inst = inst
        return self

    def _make_decorator(self, *args, **kwargs):
        def decorator(method):
            self.register(method, args, kwargs)
            return method
        return decorator

    loads = pickle.loads
    dumps = pickle.dumps

    @CachedAttr
    def registry(self):
        return []

    def dump_invoc(self, *args, **kwargs):
        return self.dumps((args, kwargs))

    def load_invoc(self, *args, **kwargs):
        return self.loads((args, kwargs))

    def register(self, method, args, kwargs):
        '''Given a single decorated handler function,
        prepare, append desired data to self.registry.
        '''
        invoc = self.dump_invoc(*args, **kwargs)
        self.registry.append((invoc, method.__name__))

    def prepare(self):
        '''Given all the registered handlers for this
        dispatcher instance, return any data required
        by the dispatch method.

        Can be overridden to provide more efficiency,
        simplicity, etc.
        '''
        return self.registry

    def gen_methods(self, *args, **kwargs):
        '''Find all method names this input dispatches to.
        '''
        dispatched = False
        for invoc, methodname in self.registry:
            args, kwargs = self.loads(invoc)
            yield getattr(self.inst, methodname), args, kwargs
            dispatched = True

        if dispatched:
            return
        msg = 'No method was found for %r on %r.'
        raise self.DispatchError(msg % ((args, kwargs), self.inst))

    def get_method(self, *args, **kwargs):
        '''Find the first method this input dispatches to.
        '''
        for method in self.gen_methods(*args, **kwargs):
            return method
        msg = 'No method was found for %r on %r.'
        raise self.DispatchError(msg % ((args, kwargs), self.inst))

    def dispatch(self, *args, **kwargs):
        '''Find and evaluate/return the first method this input dispatches to.
        '''
        for result in self.gen_dispatch(*args, **kwargs):
            return result

    def gen_dispatch(self, *args, **kwargs):
        '''Find and evaluate/yield every method this input dispatches to.
        '''
        for method_data in self.gen_methods(*args, **kwargs):
            dispatched = True
            result = self.apply_handler(method_data)
            yield from self.yield_from_handler(result)
        if dispatched:
            return
        msg = 'No method was found for %r on %r.'
        raise self.DispatchError(msg % ((args, kwargs), self.inst))

    def apply_handler(self, method_data):
        '''Call the dispatched function, optionally with other data
        stored/created during .register and .prepare
        '''
        args = ()
        kwargs = {}
        if isinstance(method_data, tuple):
            len_method = len(method_data)
            method = method_data[0]
            if 1 < len_method:
                args = method_data[1]
            if 2 < len_method:
                kwargs = method_data[2]
        else:
            method = method_data
        return method(*args, **kwargs)

    def yield_from_handler(self, result):
        '''Given an applied function result, yield from it.
        '''
        if isinstance(result, self.GeneratorType):
            yield from result
        else:
            yield result


def dedupe(gen):
    @functools.wraps(gen)
    def wrapped(*args, **kwargs):
        seen = set()
        for result in gen(*args, **kwargs):
            if result not in seen:
                seen.add(result)
                yield result
    return wrapped


class TypenameDispatcher(Dispatcher):
    '''Dispatches to a named method by inspecting the invocation, usually
    the type of the first argument.
    '''
    # It makes sense to go from general/commonplace to specific/rare,
    # so we try to dispatch by type, then bu interface, like iterableness.
    builtins = __builtins__
    types = types
    collections = collections

    abc_types = set([
        'Hashable',
        'Iterable',
        'Iterator',
        'Sized',
        'Container',
        'Callable',
        'Set',
        'MutableSet',
        'Mapping',
        'MutableMapping',
        'MappingView',
        'KeysView',
        'ItemsView',
        'ValuesView',
        'Sequence',
        'MutableSequence',
        'ByteString'])

    interp_types = set([
        'BuiltinFunctionType',
        'BuiltinMethodType',
        'CodeType',
        'DynamicClassAttribute',
        'FrameType',
        'FunctionType',
        'GeneratorType',
        'GetSetDescriptorType',
        'LambdaType',
        'MappingProxyType',
        'MemberDescriptorType',
        'MethodType',
        'ModuleType',
        'SimpleNamespace',
        'TracebackType'])

    # ------------------------------------------------------------------------
    # Plumbing.
    # ------------------------------------------------------------------------
    method_prefix = 'handle_'

    @CachedAttr
    def _method_prefix(cls):
        return getattr(cls, 'method_prefix', 'handle_')

    @CachedAttr
    def method_keys(self):
        return set(self.dispatch_data)

    # ------------------------------------------------------------------------
    # Overridables.
    # ------------------------------------------------------------------------
    def prepare(self):
        data = {}
        inst = self.inst
        prefix = self.method_prefix
        for name in dir(inst):
            if name.startswith(prefix):
                typename = name.replace(prefix, '', 1)
                data[typename] = getattr(inst, name)
        return data

    def gen_method_keys(self, *args, **kwargs):
        '''Given a node, return the string to use in computing the
        matching visitor methodname. Can also be a generator of strings.
        '''
        token = args[0]
        for mro_type in type(token).__mro__:
            yield mro_type.__name__

    @dedupe
    def gen_methods(self, *args, **kwargs):
        '''Find all method names this input dispatches to.
        '''
        token = args[0]
        dispatched = False
        data = self.dispatch_data
        for method_key in self.gen_method_keys(*args, **kwargs):
            if method_key in data:
                yield data[method_key]
                dispatched = True

        # Fall back to built-in types, then types, then collections.
        prefix = self._method_prefix
        typename = type(token).__name__
        yield from self.check_basetype(
            token, typename, self.builtins.get(typename))

        for basetype_name in (self.method_keys & self.interp_types):
            yield from self.check_basetype(
                token, basetype_name, getattr(self.types, basetype_name, None))

        for basetype_name in (self.method_keys & self.abc_types):
            yield from self.check_basetype(
                token, basetype_name, getattr(self.collections, basetype_name, None))

    def check_basetype(self, token, basetype_name, basetype):
        if basetype is None:
            return
        if not isinstance(token, basetype):
            return
        for name in (basetype_name, basetype.__name__):
            method_name = self._method_prefix + name
            method = getattr(self.inst, method_name, None)
            if method is not None:
                yield method
