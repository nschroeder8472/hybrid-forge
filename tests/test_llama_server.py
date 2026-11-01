"""Forge running `llama-server`: the profile block, and what it serves.

What this module holds to (docs/CREATIVE-TOOLS.md, *Forge runs the servers*):

1. **Opt-in by machine.** No `llamaServer` block in the profile means nothing
   changes. The block is refused in a repository config, where it would hand
   every clone one machine's port.
2. **The address decides ownership.** A model with no `baseUrl`, or loopback at
   the managed port, is forge's; anything else is external.
3. **The command line has one source.** A profile's `args` cannot repeat a flag
   forge sets, and the preset is written fresh for every start.
4. **A model the preset cannot name is refused before a server starts.**
5. **The profile keeps what it does not read.** Rewriting it must not delete a
   key a person or a newer forge put there.
6. **An installed binary is reported by its real path.** A Store Python's
   virtual `%LOCALAPPDATA%` path launches the exe with another build's DLLs.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge import llama, llama_server  # noqa: E402
from forge.config import Config, ConfigError  # noqa: E402
from forge.llama_server import LlamaServerError, Settings  # noqa: E402
from forge.profile import Profile  # noqa: E402

LOCAL = {"kind": "llamacpp", "model": "qwen3.8", "modelPath": "C:/models/qwen.gguf"}


class ProfileCase(unittest.TestCase):
    """A project and a machine profile, both in a temporary directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first so it runs last: a store a test opened must close
        # before Windows will let its directory go.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".hybridforge").mkdir(parents=True)
        self.profile = Path(self.tmp.name) / "profile.json"
        patcher = unittest.mock.patch.dict(os.environ, {"FORGE_PROFILE": str(self.profile)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, profile: dict, repo: dict | None = None) -> None:
        self.profile.write_text(json.dumps(profile), encoding="utf-8")
        body = repo if repo is not None else {}
        (self.root / ".hybridforge" / "config.json").write_text(json.dumps(body), encoding="utf-8")

    def machine(self, models: dict, server: dict | None = None) -> dict:
        roles = {role: next(iter(models)) for role in ("planner", "executor", "tester", "reviewer")}
        data: dict = {"models": models, "roles": roles}
        if server is not None:
            data["llamaServer"] = server
        return data


class OptInTests(ProfileCase):
    def test_no_block_means_forge_runs_nothing(self) -> None:
        self.write(self.machine({"qwen": LOCAL}))
        self.assertIsNone(llama_server.settings_for(Config.load(self.root)))

    def test_the_block_comes_from_the_profile(self) -> None:
        self.write(self.machine({"qwen": LOCAL}, {"port": 8081, "modelsMax": 2}))
        settings = llama_server.settings_for(Config.load(self.root))
        self.assertEqual((settings.port, settings.models_max), (8081, 2))

    def test_the_block_in_a_repository_config_is_refused(self) -> None:
        self.write(self.machine({"qwen": LOCAL}), {"llamaServer": {"port": 8080}})
        with self.assertRaises(ConfigError) as caught:
            Config.load(self.root)
        self.assertIn("machine profile", str(caught.exception))

    def test_a_bad_block_fails_config_validation(self) -> None:
        self.write(self.machine({"qwen": LOCAL}, {"modelsMax": 0}))
        with self.assertRaises(ConfigError) as caught:
            Config.load(self.root)
        self.assertIn("modelsMax", str(caught.exception))


class SettingsTests(unittest.TestCase):
    def test_defaults_are_one_checkpoint_on_8080(self) -> None:
        settings = Settings.from_block({})
        self.assertEqual((settings.port, settings.models_max, settings.args), (8080, 1, []))

    def test_args_may_not_repeat_a_flag_forge_sets(self) -> None:
        for arg in ("--models-max", "--host=0.0.0.0", "--port", "--models-preset"):
            with self.subTest(arg=arg), self.assertRaises(LlamaServerError):
                Settings.from_block({"args": [arg, "4"]})

    def test_other_args_pass_through(self) -> None:
        self.assertEqual(Settings.from_block({"args": ["--threads", "8"]}).args, ["--threads", "8"])

    def test_an_unknown_key_is_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(LlamaServerError):
            Settings.from_block({"modelMax": 1})

    def test_the_command_line_binds_loopback(self) -> None:
        settings = Settings(port=9000, models_max=1)
        argv = llama_server.argv(Path("llama-server"), Path("p.ini"), settings)
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--port") + 1], "9000")
        self.assertEqual(argv[argv.index("--models-max") + 1], "1")


class OwnershipTests(unittest.TestCase):
    def test_which_addresses_are_forges(self) -> None:
        cases = [
            ({}, 8080, True),
            ({}, 8081, False),
            ({"baseUrl": "http://127.0.0.1:8080/v1"}, 8080, True),
            ({"baseUrl": "http://localhost:8081/v1"}, 8081, True),
            ({"baseUrl": "http://127.0.0.1:9999/v1"}, 8080, False),
            ({"baseUrl": "http://gpu-host:8080/v1"}, 8080, False),
            ({"kind": "anthropic"}, 8080, False),
        ]
        for extra, port, expected in cases:
            block = {"kind": "llamacpp", **extra}
            with self.subTest(block=block, port=port):
                self.assertEqual(llama_server.is_managed(block, port), expected)


class SpecTests(ProfileCase):
    def test_a_managed_model_with_no_path_is_refused_by_name(self) -> None:
        unpathed = {"kind": "llamacpp", "model": "qwen3.8"}
        self.write(self.machine({"qwen": unpathed}, {}))
        config = Config.load(self.root)
        config.llama_server = {"port": 8080}
        with self.assertRaises(LlamaServerError) as caught:
            llama_server.spec(config, Settings())
        self.assertIn("qwen", str(caught.exception))
        self.assertIn("modelPath", str(caught.exception))

    def test_nothing_managed_means_no_spec(self) -> None:
        remote = {**LOCAL, "baseUrl": "http://gpu-host:8080/v1"}
        self.write(self.machine({"qwen": remote}))
        self.assertIsNone(llama_server.spec(Config.load(self.root), Settings()))

    def test_the_spec_writes_the_preset_and_names_it(self) -> None:
        self.write(self.machine({"qwen": LOCAL}))
        config = Config.load(self.root)
        with unittest.mock.patch.object(
            llama, "resolve_server", return_value=(Path("C:/llama/llama-server.exe"), "test")
        ):
            spec = llama_server.spec(config, Settings())
        preset = config.config_dir / "models" / "llamacpp.ini"
        self.assertTrue(preset.is_file())
        self.assertIn("[qwen3.8]", preset.read_text(encoding="utf-8"))
        self.assertIn(str(preset), spec.argv)
        self.assertTrue(spec.fingerprint)

    def test_a_changed_preset_changes_the_fingerprint(self) -> None:
        self.write(self.machine({"qwen": LOCAL}))
        config = Config.load(self.root)
        with unittest.mock.patch.object(
            llama, "resolve_server", return_value=(Path("C:/llama/llama-server.exe"), "test")
        ):
            before = llama_server.spec(config, Settings()).fingerprint
            config.models["qwen"]["contextWindow"] = 4096
            after = llama_server.spec(config, Settings()).fingerprint
        self.assertNotEqual(before, after)


class ProfileKeepsTests(ProfileCase):
    def test_a_rewrite_keeps_unknown_keys_and_the_server_block(self) -> None:
        self.profile.write_text(
            json.dumps({
                "models": {"qwen": LOCAL},
                "llamaServer": {"port": 8080},
                "comfyui": {"port": 8188},
            }),
            encoding="utf-8",
        )
        Profile.load().save()
        saved = json.loads(self.profile.read_text(encoding="utf-8"))
        self.assertEqual(saved["llamaServer"], {"port": 8080})
        self.assertEqual(saved["comfyui"], {"port": 8188})


class RealPathTests(unittest.TestCase):
    def test_an_installed_binary_is_returned_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "b1"
            build.mkdir()
            name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
            (build / name).write_text("", encoding="utf-8")
            found = llama.server_binary(build)
            self.assertEqual(found, (build / name).resolve())


if __name__ == "__main__":
    unittest.main()


class RunWiringTests(ProfileCase):
    """`forge go` starts servers only when asked, and the loop keeps them."""

    def test_a_machine_without_the_block_starts_nothing(self) -> None:
        from forge.run_servers import RunServers

        self.write(self.machine({"qwen": LOCAL}))
        self.assertIsNone(RunServers.for_config(Config.load(self.root)))

    def test_an_unstartable_config_is_refused_before_any_run(self) -> None:
        from forge.run_servers import RunServers
        from forge.servers import ServerError

        self.write(self.machine({"qwen": {"kind": "llamacpp", "model": "q"}}, {"port": 8080}))
        with self.assertRaises(ServerError):
            RunServers.for_config(Config.load(self.root))

    def test_the_loop_logs_a_restart_and_ends_on_a_second_death(self) -> None:
        from forge.loop import Orchestrator
        from forge.servers import ServerError
        from forge.state import Store

        self.write(self.machine({"qwen": LOCAL}))
        config = Config.load(self.root)
        store = Store(config.db_path)
        self.addCleanup(store.close_all)
        servers = unittest.mock.Mock()
        servers.keep.return_value = ["llama-server had exited; restarted it (pid 7)."]
        orchestrator = Orchestrator(config, store, servers=servers)
        run_id = store.create_run("goal")

        orchestrator._keep_servers(run_id)
        events = [row["message"] for row in store.events_after(0)]
        self.assertTrue(any("restarted it" in message for message in events))

        servers.keep.side_effect = ServerError("llama-server died again")
        with self.assertRaises(ServerError):
            orchestrator._keep_servers(run_id)

    def test_no_servers_means_the_check_does_nothing(self) -> None:
        from forge.loop import Orchestrator
        from forge.state import Store

        self.write(self.machine({"qwen": LOCAL}))
        config = Config.load(self.root)
        store = Store(config.db_path)
        self.addCleanup(store.close_all)
        Orchestrator(config, store)._keep_servers(store.create_run("goal"))


class LeaseTests(ProfileCase):
    """`loop.exclusiveGpu`: the card cleared for the steps that ask for it."""

    def orchestrator(self, leased: list[str]) -> tuple:
        from forge.loop import Orchestrator
        from forge.state import Store

        self.write(self.machine({"qwen": LOCAL}), {"loop": {"exclusiveGpu": leased}})
        config = Config.load(self.root)
        store = Store(config.db_path)
        self.addCleanup(store.close_all)
        return Orchestrator(config, store), store, store.create_run("goal")

    def test_an_unleasable_kind_is_refused(self) -> None:
        self.write(self.machine({"qwen": LOCAL}), {"loop": {"exclusiveGpu": ["test"]}})
        with self.assertRaises(ConfigError) as caught:
            Config.load(self.root)
        self.assertIn("capture", str(caught.exception))

    def test_a_bare_string_is_refused(self) -> None:
        self.write(self.machine({"qwen": LOCAL}), {"loop": {"exclusiveGpu": "capture"}})
        with self.assertRaises(ConfigError):
            Config.load(self.root)

    def test_an_unlisted_kind_runs_without_touching_the_card(self) -> None:
        orchestrator, _store, run_id = self.orchestrator([])
        with unittest.mock.patch("forge.gpu.clear_llama") as clear:
            with orchestrator._gpu_lease(run_id, "capture"):
                pass
        clear.assert_not_called()

    def test_a_listed_kind_clears_first_and_releases_even_when_the_step_raises(self) -> None:
        orchestrator, store, run_id = self.orchestrator(["capture"])
        with unittest.mock.patch(
            "forge.gpu.clear_llama", return_value=(["qwen3.8"], [])
        ) as clear:
            with self.assertRaises(RuntimeError):
                with orchestrator._gpu_lease(run_id, "capture", "T-1"):
                    raise RuntimeError("renderer crashed")
        clear.assert_called_once()
        messages = [row["message"] for row in store.events_after(0)]
        self.assertTrue(any("unloaded qwen3.8" in m for m in messages))
        self.assertTrue(any("released the GPU" in m for m in messages))

    def test_clearing_unloads_what_is_resident_and_never_raises(self) -> None:
        from forge import gpu

        router = unittest.mock.Mock(spec=gpu.LlamaCppProvider)
        router._router_url.return_value = "http://127.0.0.1:8080"
        router.catalog.return_value = {
            "qwen3.8": {"status": {"value": "loaded"}},
            "nemotron-3-nano": {"status": {"value": "unloaded"}},
        }
        router._status.side_effect = lambda entry: entry["status"]["value"]
        down = unittest.mock.Mock(spec=gpu.LlamaCppProvider)
        down._router_url.return_value = "http://127.0.0.1:9090"
        down.catalog.side_effect = OSError("refused")

        with unittest.mock.patch.object(gpu, "llama_routers", return_value=[router, down]):
            unloaded, problems = gpu.clear_llama(object())

        self.assertEqual(unloaded, ["qwen3.8"])
        router._unload.assert_called_once_with("qwen3.8")
        router._await_unloaded.assert_called_once_with("qwen3.8")
        self.assertEqual(len(problems), 1)
        self.assertIn("9090", problems[0])
