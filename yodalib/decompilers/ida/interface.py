import threading
import logging
from typing import Dict, Optional
from collections import OrderedDict, defaultdict
from functools import wraps

import idc
import idaapi
import ida_hexrays

import yodalib
from yodalib.api.decompiler_interface import DecompilerInterface, artifact_set_event
from yodalib.data import (
    StackVariable, Function, FunctionHeader, Struct, Comment, GlobalVariable, Enum, Patch
)
from . import compat
from .artifact_lifter import IDAArtifactLifter

_l = logging.getLogger(name=__name__)


#
#   Controller
#

class IDAInterface(DecompilerInterface):
    def __init__(self, **kwargs):
        super(IDAInterface, self).__init__(artifact_lifter=IDAArtifactLifter(self))

        # view change callback
        self._updated_ctx = None
        self._decompiler_available = None
        self._crashing_version = False

        self._max_patch_size = 0xff

    #
    # Controller Interaction
    #

    def binary_hash(self) -> str:
        return idc.retrieve_input_file_md5().hex()

    def active_context(self):
        return self._updated_ctx

    def update_active_context(self, addr):
        if not addr or addr == idaapi.BADADDR:
            return

        func_addr = compat.ida_func_addr(addr)
        if func_addr is None:
            return

        func = yodalib.data.Function(
            func_addr, 0, header=FunctionHeader(compat.get_func_name(func_addr), func_addr)
        )
        self._updated_ctx = func

    def binary_path(self) -> Optional[str]:
        return compat.get_binary_path()

    def get_func_size(self, func_addr) -> int:
        return compat.get_func_size(func_addr)

    def goto_address(self, func_addr) -> None:
        compat.jumpto(func_addr)

    @property
    def decompiler_available(self) -> bool:
        if self._decompiler_available is None:
            self._decompiler_available = ida_hexrays.init_hexrays_plugin()

        return self._decompiler_available

    def _decompile(self, function: Function) -> Optional[str]:
        try:
            cfunc = ida_hexrays.decompile(function.addr)
        except Exception:
            return None

        return str(cfunc)

    #
    # Artifact API
    #

    # functions
    def _set_function(self, func: Function, **kwargs) -> bool:
        return compat.set_function(func, headless=self.headless, decompiler_available=self.decompiler_available, **kwargs)

    def _get_function(self, addr, **kwargs) -> Optional[Function]:
        return compat.function(addr, headless=self.headless, decompiler_available=self.decompiler_available, **kwargs)

    def _functions(self) -> Dict[int, Function]:
        return compat.functions()

    # stack vars
    def _set_stack_variable(self, svar: StackVariable, **kwargs) -> bool:
        return compat.set_stack_variable(svar, headless=self.headless, decompiler_available=self.decompiler_available, **kwargs)

    # global variables
    def _set_global_variable(self, gvar: GlobalVariable, **kwargs) -> bool:
        # TODO: needs type setting implementation!
        if gvar.name:
            return compat.set_global_var_name(gvar.addr, gvar.name)

        return False

    def _get_global_var(self, addr) -> Optional[GlobalVariable]:
        return compat.global_var(addr)

    def _global_vars(self) -> Dict[int, GlobalVariable]:
        """
        Returns a dict of yodalib.GlobalVariable that contain the addr and size of each global var.
        Note: this does not contain the live data of the Artifact, only the minimum knowledge to that the Artifact
        exists. To get live data, use the singleton function of the same name.

        @return:
        """
        return compat.global_vars()

    # structs
    def _set_struct(self, struct: Struct, header=True, members=True, **kwargs) -> bool:
        data_changed = False
        if self._crashing_version and struct.name == "gcc_va_list":
            _l.critical(f"Syncing the struct {struct.name} in IDA Pro 8.2 <= will cause a crash. Skipping...")
            return False

        if header:
            data_changed |= compat.set_ida_struct(struct)

        if members:
            data_changed |= compat.set_ida_struct_member_types(struct)

        return data_changed

    def _get_struct(self, name) -> Optional[Struct]:
        return compat.struct(name)

    def _structs(self) -> Dict[str, Struct]:
        """
        Returns a dict of yodalib.Structs that contain the name and size of each struct in the decompiler.
        Note: this does not contain the live data of the Artifact, only the minimum knowledge to that the Artifact
        exists. To get live data, use the singleton function of the same name.

        @return:
        """
        return compat.structs()

    # enums
    def _set_enum(self, enum: Enum, **kwargs) -> bool:
        return compat.set_enum(enum)

    def _get_enum(self, name) -> Optional[Enum]:
        return compat.enum(name)

    def _enums(self) -> Dict[str, Enum]:
        """
        Returns a dict of yodalib.Enum that contain the name of the enums in the decompiler.
        Note: this does not contain the live data of the Artifact, only the minimum knowledge to that the Artifact
        exists. To get live data, use the singleton function of the same name.

        @return:
        """
        return compat.enums()

    # patches
    def _set_patch(self, patch: Patch, **kwargs) -> bool:
        idaapi.patch_bytes(patch.addr, patch.bytes)
        return True

    def _get_patch(self, addr) -> Optional[Patch]:
        patches = self._collect_continuous_patches(min_addr=addr-1, max_addr=addr+self._max_patch_size, stop_after_first=True)
        return patches.get(addr, None)

    def _patches(self) -> Dict[int, Patch]:
        """
        Returns a dict of yodalib.Patch that contain the addr of each Patch and the bytes.
        Note: this does not contain the live data of the Artifact, only the minimum knowledge to that the Artifact
        exists. To get live data, use the singleton function of the same name.

        @return:
        """
        return self._collect_continuous_patches()

    # comments
    def _set_comment(self, comment: Comment, **kwargs) -> bool:
        return compat.set_ida_comment(comment.addr, comment.comment, decompiled=comment.decompiled)

    def _get_comment(self, addr) -> Optional[Comment]:
        # TODO: implement me!
        return None

    def _comments(self) -> Dict[int, Comment]:
        # TODO: implement me!
        return {}

    # others...
    def _set_function_header(self, fheader: FunctionHeader, **kwargs) -> bool:
        # TODO implement me?!
        return False

    #
    # utils
    #

    def _collect_continuous_patches(self, min_addr=None, max_addr=None, stop_after_first=False) -> Dict[int, Patch]:
        patches = {}

        def _patch_collector(ea, fpos, org_val, patch_val):
            patches[ea] = bytes([patch_val])

        if min_addr is None:
            min_addr = idaapi.inf_get_min_ea()
        if max_addr is None:
            max_addr = idaapi.inf_get_max_ea()

        if min_addr is None or max_addr is None:
            return patches

        idaapi.visit_patched_bytes(min_addr, max_addr, _patch_collector)

        # now convert into continuous patches
        continuous_patches = defaultdict(bytes)
        patch_start = None
        last_pos = None
        for pos, patch in patches.items():
            should_break = False
            if last_pos is None or pos != last_pos + 1:
                patch_start = pos

                if last_pos is not None and stop_after_first:
                    should_break = True

            continuous_patches[patch_start] += patch
            if should_break:
                break

            last_pos = pos

        # convert the patches
        continuous_patches = dict(continuous_patches)
        normalized_patches = {
            offset: Patch(offset, _bytes)
            for offset, _bytes in continuous_patches.items()
        }

        return normalized_patches

