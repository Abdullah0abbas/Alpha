import ast
import builtins
import inspect
import logging
import os
import pathlib
import sys
import tempfile
import unittest
from types import ModuleType
from unittest.mock import MagicMock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET_DIRS = ("bot", "db", "config", "watcher")
BUILTIN_NAMES = set(dir(builtins)) | {
    "__name__",
    "__file__",
    "__doc__",
    "__package__",
    "__spec__",
    "__builtins__",
}


def _collect_assigned_targets(node) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            names |= _collect_assigned_targets(elt)
    elif isinstance(node, ast.Starred):
        names |= _collect_assigned_targets(node.value)
    return names


def _collect_comp_names(node) -> set[str]:
    names: set[str] = set()
    for gen in node.generators:
        names |= _collect_assigned_targets(gen.target)
    return names


def _find_undefined_names(tree: ast.Module) -> list[tuple[int, str]]:
    issues: list[tuple[int, str]] = []

    def walk_body(body, scope_names: set[str]):
        all_local_defs = set(scope_names)

        class DefCollector(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                all_local_defs.add(n.name)

            def visit_AsyncFunctionDef(self, n):
                all_local_defs.add(n.name)

            def visit_ClassDef(self, n):
                all_local_defs.add(n.name)

            def visit_Import(self, n):
                for a in n.names:
                    all_local_defs.add(a.asname or a.name.split(".")[0])

            def visit_ImportFrom(self, n):
                for a in n.names:
                    if a.name != "*":
                        all_local_defs.add(a.asname or a.name)

            def visit_Assign(self, n):
                for t in n.targets:
                    all_local_defs.update(_collect_assigned_targets(t))
                self.generic_visit(n)

            def visit_AugAssign(self, n):
                all_local_defs.update(_collect_assigned_targets(n.target))
                self.generic_visit(n)

            def visit_AnnAssign(self, n):
                all_local_defs.update(_collect_assigned_targets(n.target))
                self.generic_visit(n)

            def visit_For(self, n):
                all_local_defs.update(_collect_assigned_targets(n.target))
                self.generic_visit(n)

            def visit_AsyncFor(self, n):
                all_local_defs.update(_collect_assigned_targets(n.target))
                self.generic_visit(n)

            def visit_With(self, n):
                for item in n.items:
                    if item.optional_vars:
                        all_local_defs.update(_collect_assigned_targets(item.optional_vars))
                self.generic_visit(n)

            def visit_AsyncWith(self, n):
                for item in n.items:
                    if item.optional_vars:
                        all_local_defs.update(_collect_assigned_targets(item.optional_vars))
                self.generic_visit(n)

            def visit_ExceptHandler(self, n):
                if n.name:
                    all_local_defs.add(n.name)
                self.generic_visit(n)

            def visit_NamedExpr(self, n):
                all_local_defs.update(_collect_assigned_targets(n.target))
                self.generic_visit(n)

        for stmt in body:
            DefCollector().visit(stmt)

        class RefChecker(ast.NodeVisitor):
            def __init__(self, current_scope: set[str]):
                self.current_scope = current_scope

            def visit_Name(self, n):
                if isinstance(n.ctx, ast.Load):
                    if n.id not in self.current_scope and n.id not in BUILTIN_NAMES:
                        issues.append((n.lineno, n.id))

            def visit_FunctionDef(self, n):
                for d in n.decorator_list:
                    self.visit(d)
                for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
                    self.visit(d)
                for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
                    if a.annotation:
                        self.visit(a.annotation)
                if n.args.vararg and n.args.vararg.annotation:
                    self.visit(n.args.vararg.annotation)
                if n.args.kwarg and n.args.kwarg.annotation:
                    self.visit(n.args.kwarg.annotation)
                if n.returns:
                    self.visit(n.returns)

                fn_scope = set(self.current_scope)
                for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
                    fn_scope.add(a.arg)
                if n.args.vararg:
                    fn_scope.add(n.args.vararg.arg)
                if n.args.kwarg:
                    fn_scope.add(n.args.kwarg.arg)
                walk_body(n.body, fn_scope)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                for b in n.bases + n.decorator_list:
                    self.visit(b)
                for kw in n.keywords:
                    self.visit(kw.value)
                walk_body(n.body, self.current_scope)

            def visit_Lambda(self, n):
                lam_scope = set(self.current_scope)
                for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
                    lam_scope.add(a.arg)
                if n.args.vararg:
                    lam_scope.add(n.args.vararg.arg)
                if n.args.kwarg:
                    lam_scope.add(n.args.kwarg.arg)
                for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
                    self.visit(d)
                RefChecker(lam_scope).visit(n.body)

            def visit_ListComp(self, n):
                cs = set(self.current_scope) | _collect_comp_names(n)
                for gen in n.generators:
                    RefChecker(cs).visit(gen.iter)
                    for ifc in gen.ifs:
                        RefChecker(cs).visit(ifc)
                RefChecker(cs).visit(n.elt)

            visit_SetComp = visit_ListComp
            visit_GeneratorExp = visit_ListComp

            def visit_DictComp(self, n):
                cs = set(self.current_scope) | _collect_comp_names(n)
                for gen in n.generators:
                    RefChecker(cs).visit(gen.iter)
                    for ifc in gen.ifs:
                        RefChecker(cs).visit(ifc)
                RefChecker(cs).visit(n.key)
                RefChecker(cs).visit(n.value)

        checker = RefChecker(all_local_defs)
        for stmt in body:
            checker.visit(stmt)

    walk_body(tree.body, set())
    return sorted(set(issues))


def _make_stub_module(name: str, is_pkg: bool = True) -> ModuleType:
    mod = ModuleType(name)
    if is_pkg:
        mod.__path__ = []
    return mod


class _MagicFilter:
    def __getattr__(self, item):
        return _MagicFilter()

    def __call__(self, *args, **kwargs):
        return _MagicFilter()

    def __eq__(self, other):
        return _MagicFilter()

    def __ne__(self, other):
        return _MagicFilter()

    def __and__(self, other):
        return _MagicFilter()

    def __or__(self, other):
        return _MagicFilter()

    def __invert__(self):
        return _MagicFilter()


class _StubRouter:
    def __init__(self, name: str | None = None):
        self.name = name
        self.sub_routers = []

    def include_router(self, router):
        self.sub_routers.append(router)
        return router

    def include_routers(self, *routers):
        for r in routers:
            self.include_router(r)

    def _pass_decorator(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    message = _pass_decorator
    callback_query = _pass_decorator
    inline_query = _pass_decorator
    errors = _pass_decorator


class _StubState:
    def __init__(self, state=None, group_name=None):
        self.state = state

    def __set_name__(self, owner, name):
        self.state = f"{owner.__name__}:{name}"


class _StubStatesGroup:
    pass


class _StubBaseMiddleware:
    pass


def _install_missing_runtime_stubs() -> bool:
    """
    Installs in-memory stubs for external packages (aiogram, sqlalchemy) ONLY when
    the real packages are absent (e.g. sandbox/CI without venv).
    Returns True if stubs were installed, False if real packages are installed.
    """
    try:
        import aiogram  # noqa: F401
        import sqlalchemy  # noqa: F401
        if isinstance(aiogram, MagicMock) or isinstance(sqlalchemy, MagicMock):
            raise ImportError("Partial test mock detected")
        return False
    except ImportError:
        pass

    # Clear partially mocked modules from earlier tests so imports behave uniformly
    for prefix in ("aiogram", "sqlalchemy", "bot", "db", "config"):
        for mod_name in list(sys.modules.keys()):
            if mod_name == prefix or mod_name.startswith(prefix + "."):
                sys.modules.pop(mod_name, None)

    # ---- aiogram stubs ----
    aiogram_mod = _make_stub_module("aiogram")
    aiogram_mod.Bot = MagicMock
    aiogram_mod.Dispatcher = MagicMock
    aiogram_mod.Router = _StubRouter
    aiogram_mod.F = _MagicFilter()
    aiogram_mod.BaseMiddleware = _StubBaseMiddleware

    class _MockCallableClass:
        def __init__(self, *args, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    aiogram_types = _make_stub_module("aiogram.types", is_pkg=False)
    for tname in (
        "BotCommand",
        "BotCommandScopeDefault",
        "BotCommandScopeAllPrivateChats",
        "BotCommandScopeChat",
        "CallbackQuery",
        "Message",
        "InlineKeyboardMarkup",
        "InlineKeyboardButton",
        "Update",
        "ErrorEvent",
        "FSInputFile",
        "TelegramObject",
        "User",
    ):
        setattr(aiogram_types, tname, _MockCallableClass)

    aiogram_fsm = _make_stub_module("aiogram.fsm")
    aiogram_fsm_context = _make_stub_module("aiogram.fsm.context", is_pkg=False)
    aiogram_fsm_context.FSMContext = MagicMock
    aiogram_fsm_state = _make_stub_module("aiogram.fsm.state", is_pkg=False)
    aiogram_fsm_state.State = _StubState
    aiogram_fsm_state.StatesGroup = _StubStatesGroup
    aiogram_fsm_storage = _make_stub_module("aiogram.fsm.storage")
    aiogram_fsm_memory = _make_stub_module("aiogram.fsm.storage.memory", is_pkg=False)
    aiogram_fsm_memory.MemoryStorage = MagicMock

    aiogram_filters = _make_stub_module("aiogram.filters", is_pkg=True)
    aiogram_filters.Command = _MockCallableClass
    aiogram_filters.CommandStart = _MockCallableClass
    aiogram_filters.CommandObject = _MockCallableClass

    aiogram_filters_command = _make_stub_module("aiogram.filters.command", is_pkg=False)
    aiogram_filters_command.CommandObject = _MockCallableClass
    aiogram_filters_command.Command = _MockCallableClass
    aiogram_filters_command.CommandStart = _MockCallableClass

    aiogram_exceptions = _make_stub_module("aiogram.exceptions", is_pkg=False)
    for exc_name in (
        "TelegramBadRequest",
        "TelegramForbiddenError",
        "TelegramAPIError",
        "TelegramNetworkError",
        "TelegramRetryAfter",
        "RestartingTelegram",
    ):
        setattr(aiogram_exceptions, exc_name, type(exc_name, (Exception,), {}))

    aiogram_utils = _make_stub_module("aiogram.utils")
    aiogram_utils_kb = _make_stub_module("aiogram.utils.keyboard", is_pkg=False)
    aiogram_utils_kb.InlineKeyboardBuilder = MagicMock

    sys.modules.update({
        "aiogram": aiogram_mod,
        "aiogram.types": aiogram_types,
        "aiogram.fsm": aiogram_fsm,
        "aiogram.fsm.context": aiogram_fsm_context,
        "aiogram.fsm.state": aiogram_fsm_state,
        "aiogram.fsm.storage": aiogram_fsm_storage,
        "aiogram.fsm.storage.memory": aiogram_fsm_memory,
        "aiogram.filters": aiogram_filters,
        "aiogram.filters.command": aiogram_filters_command,
        "aiogram.exceptions": aiogram_exceptions,
        "aiogram.utils": aiogram_utils,
        "aiogram.utils.keyboard": aiogram_utils_kb,
    })

    # ---- sqlalchemy stubs ----
    class _StubDeclarativeBase:
        metadata = MagicMock()

    class _StubMapped:
        def __class_getitem__(cls, item):
            return item

    sa_mod = _make_stub_module("sqlalchemy")
    for sname in (
        "Column",
        "Integer",
        "BigInteger",
        "String",
        "Numeric",
        "Boolean",
        "DateTime",
        "ForeignKey",
        "Index",
        "CheckConstraint",
        "UniqueConstraint",
        "Text",
        "or_",
        "and_",
        "not_",
        "func",
        "text",
        "update",
        "delete",
        "desc",
        "asc",
        "cast",
        "case",
        "literal",
        "exists",
    ):
        setattr(sa_mod, sname, MagicMock())
    sa_mod.select = MagicMock()

    sa_ext = _make_stub_module("sqlalchemy.ext")
    sa_ext_async = _make_stub_module("sqlalchemy.ext.asyncio", is_pkg=False)
    sa_ext_async.AsyncSession = MagicMock
    sa_ext_async.async_sessionmaker = MagicMock()
    sa_ext_async.create_async_engine = MagicMock()

    sa_future = _make_stub_module("sqlalchemy.future", is_pkg=False)
    sa_future.select = MagicMock()

    sa_orm = _make_stub_module("sqlalchemy.orm", is_pkg=False)
    sa_orm.DeclarativeBase = _StubDeclarativeBase
    sa_orm.Mapped = _StubMapped
    sa_orm.mapped_column = MagicMock()
    sa_orm.declarative_base = lambda: _StubDeclarativeBase
    sa_orm.relationship = MagicMock()
    sa_orm.selectinload = MagicMock()

    sa_exc = _make_stub_module("sqlalchemy.exc", is_pkg=False)
    sa_exc.IntegrityError = type("IntegrityError", (Exception,), {})
    sa_exc.SQLAlchemyError = type("SQLAlchemyError", (Exception,), {})

    sa_dialects = _make_stub_module("sqlalchemy.dialects")
    sa_pg = _make_stub_module("sqlalchemy.dialects.postgresql", is_pkg=False)
    sa_pg.JSONB = MagicMock()
    sa_pg.ARRAY = MagicMock()

    sys.modules.update({
        "sqlalchemy": sa_mod,
        "sqlalchemy.ext": sa_ext,
        "sqlalchemy.ext.asyncio": sa_ext_async,
        "sqlalchemy.future": sa_future,
        "sqlalchemy.orm": sa_orm,
        "sqlalchemy.exc": sa_exc,
        "sqlalchemy.dialects": sa_dialects,
        "sqlalchemy.dialects.postgresql": sa_pg,
    })

    return True


class TestStartupAndImports(unittest.TestCase):
    def test_all_python_files_compile_and_have_no_undefined_names(self):
        for base_dir in TARGET_DIRS:
            dir_path = PROJECT_ROOT / base_dir
            for py_file in sorted(dir_path.rglob("*.py")):
                rel = py_file.relative_to(PROJECT_ROOT)
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(rel))
                undefined = _find_undefined_names(tree)
                self.assertEqual(
                    undefined,
                    [],
                    msg=f"Undefined symbol(s) detected in {rel}: {undefined}",
                )

    def test_no_intra_bot_required_arg_mismatches(self):
        """
        Ensures no function in bot/ calls another module function (local or imported from bot.*)
        while omitting required positional/keyword arguments.
        """
        module_defs: dict[str, dict[str, tuple[list[str], int, set[str], bool]]] = {}
        module_imports: dict[str, dict[str, tuple[str, str]]] = {}
        module_trees: dict[str, ast.Module] = {}

        for py_file in sorted((PROJECT_ROOT / "bot").rglob("*.py")):
            rel = py_file.relative_to(PROJECT_ROOT)
            mod_name = ".".join(rel.with_suffix("").parts)
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(rel))
            module_trees[mod_name] = tree

            defs: dict[str, tuple[list[str], int, set[str], bool]] = {}
            imports: dict[str, tuple[str, str]] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pos_args = [a.arg for a in node.args.posonlyargs + node.args.args]
                    num_defaults = len(node.args.defaults)
                    req_pos_count = len(pos_args) - num_defaults
                    req_pos_names = pos_args[:req_pos_count]
                    req_kw = {
                        a.arg
                        for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults)
                        if d is None
                    }
                    has_var = bool(node.args.vararg or node.args.kwarg)
                    defs[node.name] = (req_pos_names, len(pos_args), req_kw, has_var)
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bot."):
                    for alias in node.names:
                        imports[alias.asname or alias.name] = (node.module, alias.name)
            module_defs[mod_name] = defs
            module_imports[mod_name] = imports

        mismatches: list[str] = []
        for mod_name, tree in module_trees.items():
            local_defs = module_defs[mod_name]
            imp_map = module_imports[mod_name]

            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    fname = node.func.id
                    sig = local_defs.get(fname)
                    if sig is None and fname in imp_map:
                        target_mod, orig_name = imp_map[fname]
                        sig = module_defs.get(target_mod, {}).get(orig_name)
                    if sig is None:
                        continue
                    req_pos_names, _max_pos, req_kw, has_var = sig
                    if has_var or any(isinstance(a, ast.Starred) for a in node.args) or any(
                        kw.arg is None for kw in node.keywords
                    ):
                        continue
                    kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
                    unfilled_pos = [
                        arg_name
                        for idx, arg_name in enumerate(req_pos_names)
                        if idx >= len(node.args) and arg_name not in kw_names
                    ]
                    if unfilled_pos:
                        mismatches.append(
                            f"{mod_name}:{node.lineno} calls {fname}() missing required arg(s): {unfilled_pos}"
                        )
                    missing_kw = req_kw - kw_names
                    if missing_kw:
                        mismatches.append(
                            f"{mod_name}:{node.lineno} calls {fname}() missing required kw-only arg(s): {sorted(missing_kw)}"
                        )

        self.assertEqual(mismatches, [], msg="\n".join(mismatches))

    def test_architecture_and_regression_invariants(self):
        offers_src = (PROJECT_ROOT / "bot/handlers/offers.py").read_text(encoding="utf-8")
        self.assertNotIn("from bot.handlers.deals import update_channel_offer_message", offers_src)
        self.assertIn("from bot.services.channel import update_channel_offer_message", offers_src)

        deals_creation_src = (PROJECT_ROOT / "bot/handlers/deals/creation.py").read_text(encoding="utf-8")
        deals_creation_tree = ast.parse(deals_creation_src)
        imported_typing = {
            alias.name
            for node in deals_creation_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "typing"
            for alias in node.names
        }
        self.assertIn("Optional", imported_typing)
        self.assertIn("from bot.utils.payment_prompts import", deals_creation_src)

        deals_payment_info_src = (PROJECT_ROOT / "bot/handlers/deals/payment_info.py").read_text(encoding="utf-8")
        self.assertIn("from bot.utils.payment_prompts import", deals_payment_info_src)

        profile_src = (PROJECT_ROOT / "bot/handlers/profile.py").read_text(encoding="utf-8")
        self.assertIn("from bot.utils.payment_prompts import", profile_src)

        flow_states_src = (PROJECT_ROOT / "bot/states/flow_states.py").read_text(encoding="utf-8")
        for dead_state in (
            "confirm_rate",
            "confirm_min_limit",
            "confirm_max_limit",
            "waiting_for_payout_address",
        ):
            self.assertNotIn(dead_state, flow_states_src)

    def test_full_startup_import_smoke(self):
        saved_modules = sys.modules.copy()
        saved_path = list(sys.path)
        saved_cwd = os.getcwd()
        audit_logger = logging.getLogger("audit")
        saved_audit_handlers = list(audit_logger.handlers)

        try:
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            with tempfile.TemporaryDirectory() as tmpdir:
                os.chdir(tmpdir)
                _install_missing_runtime_stubs()

                import bot.main as bot_main
                from bot.handlers import admin, base, deals, negotiations, offers, profile
                import bot.tasks as bot_tasks

                self.assertTrue(inspect.iscoroutinefunction(bot_main.main))
                self.assertTrue(inspect.iscoroutinefunction(bot_tasks.start_expiry_watcher))
                self.assertTrue(inspect.iscoroutinefunction(bot_tasks.start_daily_backup_scheduler))

                for mod, router_attr in (
                    (base, "base_router"),
                    (offers, "offers_router"),
                    (deals, "deals_router"),
                    (negotiations, "negotiations_router"),
                    (admin, "admin_router"),
                    (profile, "profile_router"),
                ):
                    self.assertTrue(hasattr(mod, router_attr), msg=f"Missing {router_attr} in {mod.__name__}")

                for deals_export in (
                    "deals_router",
                    "my_deals",
                    "derive_escrow_address",
                    "apply_seller_payment_info",
                    "forward_proof_photo_to_seller",
                    "apply_buyer_payout_wallet",
                ):
                    self.assertTrue(hasattr(deals, deals_export), msg=f"Missing export {deals_export} in bot.handlers.deals")

                if hasattr(deals.deals_router, "sub_routers"):
                    self.assertEqual(len(deals.deals_router.sub_routers), 4)

                cq_sig = inspect.signature(base.cq_create_offer)
                self.assertIn("db_session", cq_sig.parameters)
        finally:
            os.chdir(saved_cwd)
            sys.path[:] = saved_path
            for h in list(audit_logger.handlers):
                if h not in saved_audit_handlers:
                    audit_logger.removeHandler(h)
                    try:
                        h.close()
                    except Exception:
                        pass
            sys.modules.clear()
            sys.modules.update(saved_modules)


if __name__ == "__main__":
    unittest.main()
