import gc
import os
from collections import defaultdict
from contextlib import contextmanager

from os.path import abspath
from os.path import basename
from os.path import dirname
from os.path import join
from typing import Iterable, Union

import bsddb3 as bsddb


class SetWithCallback(set):
    """A set implementation that triggers callbacks on `add` or `update`.

    It has an important use in BerkleyHashSet database implementation:
    it allows a user to modify sets like native Python's structures while all
    the changes are forwarded to the database, without additional user's action.
    """
    _modifying_methods = {'update', 'add'}

    def __init__(self, items, callback):
        super().__init__(items)
        self.callback = callback
        for method_name in self._modifying_methods:
            method = getattr(self, method_name)
            setattr(self, method_name, self._wrap_method(method))

    def _wrap_method(self, method):
        def new_method_with_callback(*args, **kwargs):
            result = method(*args, **kwargs)
            self.callback(self)
            return result
        return new_method_with_callback


class BerkleyDatabaseNotOpened(Exception):
    pass


def require_open(func):
    def func_working_only_if_db_is_open(self, *args, **kwargs):
        if not self.is_open:
            raise BerkleyDatabaseNotOpened
        return func(self, *args, **kwargs)
    return func_working_only_if_db_is_open


class BerkleyHashSet:
    """A hash-indexed database where values are equivalent to Python's sets.

    It uses Berkley database for storage and accesses it through bsddb3 module.
    """

    def __init__(self, name=None, integer_values=False):
        self.is_open = False
        self.integer_values = integer_values
        if name:
            self.open(name)

    def _create_path(self):
        """Returns path to a file containing the database.

        The file is not guaranteed to exist, although the 'databases' directory
        will be created (if it does not exist).
        """
        base_dir = abspath(dirname(__file__))
        db_dir = join(base_dir, dirname(self.name))
        os.makedirs(db_dir, exist_ok=True)
        return join(db_dir, basename(self.name))

    def open(self, name, mode='c', cache_size=20480 * 8):
        """Open hash database in a given mode.

        By default it opens a database in read-write mode and in case
        if a database of given name does not exists it creates one.
        """
        self.name = name
        self.path = self._create_path()
        self.db = bsddb.hashopen(self.path, mode, cachesize=cache_size)
        self.is_open = True

    def close(self):
        self.db.close()

    @require_open
    def __getitem__(self, key) -> set:
        """key: has to be str"""

        key = bytes(key, 'utf-8')

        try:
            items = filter(
                bool,
                self.db.get(key).decode().split('|')
            )
            if self.integer_values:
                items = map(int, items)
        except (KeyError, AttributeError):
            items = []

        return SetWithCallback(
            items,
            lambda new_set: self.__setitem__(key, new_set)
        )

    def items(self):
        """Yields (key, iterator over items from value set) tuples.

        All atomic elements are returned as plain strings.
        """
        decode = bytes.decode
        split = str.split
        for key, value in self.db.iteritems():
            try:
                yield key.decode(), filter(bool, split(decode(value), '|'))
            except (KeyError, AttributeError):
                pass

    def values(self):
        """Yields iterators over items from value set.

        All atomic elements are returned as plain strings.
        """
        decode = bytes.decode
        split = str.split
        for key, value in self.db.iteritems():
            try:
                yield filter(bool, split(decode(value), '|'))
            except (KeyError, AttributeError):
                pass

    def update(self, key, value):
        key = bytes(key, 'utf-8')
        try:
            items = set(
                filter(
                    bool,
                    self.db.get(key).split(b'|')
                )
            )
        except (KeyError, AttributeError):
            items = set()

        assert '|' not in value
        items.update((bytes(v, 'utf-8') for v in value))

        self.db[key] = b'|'.join(items)

    def _get(self, key):
        try:
            items = set(
                filter(
                    bool,
                    self.db.get(key).split(b'|')
                )
            )
        except (KeyError, AttributeError):
            items = set()
        return items

    def add(self, key, value):
        key = bytes(key, 'utf-8')
        items = self._get(key)
        assert '|' not in value
        items.add(bytes(value, 'utf-8'))
        self.db[key] = b'|'.join(items)

    @require_open
    def __setitem__(self, key: Union[str, bytes], items: Iterable[Union[str, int]]):
        if self.integer_values:
            items = map(str, items)
        else:
            assert all('|' not in item for item in items)
        if not isinstance(key, bytes):
            key = bytes(key, 'utf-8')
        self.db[key] = bytes('|'.join(items), 'utf-8')

    @require_open
    def __len__(self):
        return len(self.db)

    @require_open
    def drop(self, not_exists_ok=True):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            if not_exists_ok:
                pass
            else:
                raise

    @require_open
    def reset(self):
        """Reset database completely by its removal and recreation."""
        self.drop()
        self.open(self.name)

    @require_open
    def reload(self):
        self.close()
        self.open(self.name)


class BerkleyHashSetWithCache(BerkleyHashSet):

    def __init__(self, name=None, integer_values=False):
        self.in_cached_session = False
        self.keys_on_disk = None
        self.cache = {}
        self.i = None
        super().__init__(name=name, integer_values=integer_values)

    def _cached_get_with_old_values(self, key):
        cache = self.cache
        if key in cache:
            return cache[key]
        else:
            items = self._get(key)
            cache[key] = items
            return items

    def _cached_get_ignore_old_values(self, key):
        if key in self.keys_on_disk:
            return self._get(key)
        return self.cache[key]

    _cached_get = _cached_get_with_old_values

    def cached_add(self, key: str, value: str):
        items = self._cached_get(bytes(key, 'utf-8'))
        items.add(bytes(value, 'utf-8'))

    def cached_add_integer(self, key: str, value: int):
        items = self._cached_get(bytes(key, 'utf-8'))
        items.add(value)

    def flush_cache(self):
        assert self.in_cached_session

        if self.integer_values:
            for key, items in self.cache.items():
                value = '|'.join(map(str, items))
                self.db[key] = bytes(value, 'utf-8')
        else:
            for key, items in self.cache.items():
                self.db[key] = b'|'.join(items)

        self.keys_on_disk.update(self.cache.keys())
        self.cache = defaultdict(set)

        self.i += 1
        if self.i % 10 == 9:
            gc.collect()

    @contextmanager
    def cached_session(self, overwrite_db_values=False):
        self.i = 0
        old_cache = self.cache
        old_cached_get = self._cached_get
        self.in_cached_session = True
        self.keys_on_disk = set()
        self.cache = defaultdict(set)

        self._cached_get = (
            self._cached_get_ignore_old_values
            if overwrite_db_values else
            self._cached_get_with_old_values
        )

        yield

        print('Flushing changes')

        self.flush_cache()
        self.cache = old_cache
        self._cached_get = old_cached_get
        self.in_cached_session = False
