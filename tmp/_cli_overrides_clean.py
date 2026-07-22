# === BEGIN BLOCK: clean replacement for cli_overrides ===
# Section heading + module-level constants + helpers + _build_cli_overrides.
# This self-contained snippet is spliced into dashboard/server.py to replace
# the broken concatenation across multiple str_replace iterations.

# ---- /api/cli_overrides ----------------------------------------------------
# One-glance provenance for every config key. Walks the merge chain
# (config.yaml -> config.local.yaml -> active profile -> in-memory layers ->
# state overrides) and shows WHERE each key came from (file+line) so the
# next time a profile shadow beats a base edit, the user sees it instantly.
# Mtime-cached for 5s; cheap YAML walk triggered per poll once active profile
# or override files change.
_CLI_OVERRIDES_CACHE = {}
_CLI_OVERRIDES_CACHE_TTL_SEC = 5.0
_CLI_OVERRIDES_CACHE_LOCK = threading.Lock()


def _cli_overrides_mtime_iso(path):
    try:
        import os as _os
        mtime = _os.path.getmtime(path)
    except OSError:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def _flatten_config_keys(value, prefix=""):
    """Flatten dicts into dot-path -> Python-value. Sequences stay at the
    parent path because core.utils._deep_merge replaces lists as atomic
    values when they overlap between layers."""
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            out[child] = v
            if isinstance(v, dict):
                out.update(_flatten_config_keys(v, child))
    return out


def _collect_yaml_layers():
    """Walk the YAML merge chain: base -> local -> active profile. Each layer
    carries its file path, source-line line_map, and a UTC mtime stamp."""
    layers = []
    base_path = ROOT / "config.yaml"
    if base_path.is_file():
        try:
            value, lines = _yaml_load_with_lines(base_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _LOG.warning("cli_overrides: base load failed: %s", exc)
            value, lines = {}, {}
        layers.append({
            "name": "base",
            "source_path": str(base_path),
            "value": value,
            "lines": lines,
            "in_memory": False,
            "override_time": _cli_overrides_mtime_iso(base_path),
        })
    local_path = ROOT / "config.local.yaml"
    if local_path.is_file():
        try:
            value, lines = _yaml_load_with_lines(local_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _LOG.warning("cli_overrides: local load failed: %s", exc)
            value, lines = {}, {}
        layers.append({
            "name": "local",
            "source_path": str(local_path),
            "value": value,
            "lines": lines,
            "in_memory": False,
            "override_time": _cli_overrides_mtime_iso(local_path),
        })
    try:
        from core.profile_launcher import active_profile_name
        prof = active_profile_name()
        if prof:
            prof_path = ROOT / "profiles" / f"{prof}.yaml"
            if prof_path.is_file():
                try:
                    value, lines = _yaml_load_with_lines(prof_path)
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    _LOG.warning("cli_overrides: profile load failed: %s", exc)
                    value, lines = {}, {}
                layers.append({
                    "name": f"profile:{prof}",
                    "source_path": str(prof_path),
                    "value": value,
                    "lines": lines,
                    "in_memory": False,
                    "override_time": _cli_overrides_mtime_iso(prof_path),
                })
    except Exception as exc:
        _LOG.debug("cli_overrides: profile discovery skipped: %s", exc)
    return layers


def _collect_state_overrides():
    """Read the two state-level override files. They don't carry per-key line
    info (records are JSON), but they DO carry override_time and a name so
    the UI can call them out when they touch config keys."""
    out = []
    try:
        lco = read_json_state("learning_config_overrides.json", default=None)
        if isinstance(lco, dict):
            patches = lco.get("patches") or []
            rollbacks = lco.get("rollbacks") or []
            ops_total = 0
            try:
                ops_total = sum(len(p.get("ops") or []) for p in patches if isinstance(p, dict))
            except (TypeError, AttributeError):
                ops_total = 0
            if patches or rollbacks or ops_total or lco.get("updated_at"):
                patch_paths = []
                for p in patches:
                    if not isinstance(p, dict):
                        continue
                    pp = p.get("patch") or {}
                    if isinstance(pp.get("path"), list):
                        patch_paths.append(".".join(str(x) for x in pp["path"]))
                out.append({
                    "name": "state_overrides:learning_config_overrides",
                    "source_path": str(STATE_DIR / "learning_config_overrides.json"),
                    "value": {
                        "_patches_count": len(patches),
                        "_rollbacks_count": len(rollbacks),
                        "_ops_count": ops_total,
                        "_patch_paths": patch_paths[-30:],
                    },
                    "lines": {},
                    "in_memory": True,
                    "override_time": lco.get("updated_at"),
                })
    except Exception as exc:
        _LOG.debug("cli_overrides: learning_config_overrides read failed: %s", exc)
    try:
        be = read_json_state("symbol_be_trail_live.json", default=None)
        if isinstance(be, dict):
            syms = be.get("symbols") or {}
            if isinstance(syms, dict) and syms:
                out.append({
                    "name": "state_overrides:symbol_be_trail_live",
                    "source_path": str(STATE_DIR / "symbol_be_trail_live.json"),
                    "value": {"_symbols_count": len(syms)},
                    "lines": {},
                    "in_memory": True,
                    "override_time": be.get("updated_at"),
                })
    except Exception as exc:
        _LOG.debug("cli_overrides: symbol_be_trail_live read failed: %s", exc)
    return out


# In-memory layer catalog. Each entry has a predicate that decides whether
# the layer ACTUALLY fired in this session (vs being declared-but-skipped).
# safety_relevant=True flips the exception fallback default to False so a
# thrown predicate never lets us falsely claim a safety layer ran.
_IN_MEMORY_LAYER_CATALOG_TEMPLATE = [
    {"name": "in_memory:blue_guardian",
     "source_path": "core/blue_guardian.py:prepare_blue_guardian_profile",
     "note": "blue_guardian profile synthesis",
     "safety_relevant": True,
     "enabled_predicate": lambda c: bool((c.get("blue_guardian") or {}).get("enabled", False))},
    {"name": "in_memory:performance_gates",
     "source_path": "core/performance_benchmark.py:sync_performance_gates",
     "note": "performance.apply_when gate",
     "safety_relevant": False,
     "enabled_predicate": lambda c: ((c.get("performance") or {}).get("apply_when") or "") not in ("", "never", "false")},
    {"name": "in_memory:practice_gates_pre",
     "source_path": "core/practice_session.py:sync_practice_gates",
     "note": "first practice-gate sync (before micro_profile)",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:arena_symbols",
     "source_path": "core/strategy_arena.py:sync_arena_symbols",
     "note": "strategy_arena active symbols",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:micro_profile",
     "source_path": "core/micro_profile.py:sync_micro_profile",
     "note": "micro account profile synthesis",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:practice_gates_post",
     "source_path": "core/practice_session.py:sync_practice_gates",
     "note": "second practice-gate sync (after micro_profile)",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:config_overrides",
     "source_path": "core/blue_guardian.py:apply_config_overrides",
     "note": "blue_guardian apply_config_overrides",
     "safety_relevant": True,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:learning_overrides",
     "source_path": "core/learning_overrides.py:apply_learning_overrides",
     "note": "Phase 2.4 live_apply_limited patches",
     "safety_relevant": True,
     "enabled_predicate": lambda c: (c.get("learning") or {}).get("mode") == "live_apply_limited"},
]


def _compute_in_memory_layers(config):
    """Return the in-memory layer catalog filtered by what ACTUALLY fired."""
    out = []
    cfg = config if isinstance(config, dict) else {}
    for entry in _IN_MEMORY_LAYER_CATALOG_TEMPLATE:
        try:
            enabled = bool(entry["enabled_predicate"](cfg))
        except Exception:
            enabled = not bool(entry.get("safety_relevant", False))
        out.append({
            "name": entry["name"],
            "source_path": entry["source_path"],
            "note": entry["note"],
            "enabled": enabled,
            "safety_relevant": bool(entry.get("safety_relevant", False)),
        })
    return out


def _build_cli_overrides(*, force=False):
    """Per-key provenance view of the entire config merge chain.

    Mtime-cached for ``_CLI_OVERRIDES_CACHE_TTL_SEC``. Use ``force=True`` from
    anything that wants to bypass the cache (e.g. an admin button).

    Returns a JSON-friendly dict with:
      - updated_at, active_profile
      - layers: ordered input chain summary (name + source_path + key_count)
      - overrides: per-key history (key, value, base_value, winning_layer,
        source_path, line_number, override_time, history[])
      - by_layer: dict[layer_name] -> list of keys that layer last won
      - hot_conflicts: keys with 3+ distinct values across YAML layers
      - summary: aggregate counts
    """
    now = time.time()
    cache_key = "_cli_overrides_v1"
    cached = _CLI_OVERRIDES_CACHE.get(cache_key)
    if not force and cached and (now - cached["_ts"]) < _CLI_OVERRIDES_CACHE_TTL_SEC:
        return cached["payload"]

    try:
        yaml_layers = _collect_yaml_layers()
        state_layers = _collect_state_overrides()
    except Exception as exc:
        _LOG.warning("cli_overrides: layer discovery failed: %s", exc)
        yaml_layers, state_layers = [], []

    try:
        merged_cfg = load_config() or {}
        if not isinstance(merged_cfg, dict):
            merged_cfg = {}
    except Exception as exc:
        _LOG.warning(
            "cli_overrides: load_config() failed: %s; in-memory predicates defaulting to enabled=True",
            exc,
        )
        merged_cfg = {}

    flat_layers = []
    base_flat = {}
    for layer in yaml_layers:
        flat = _flatten_config_keys(layer.get("value") or {})
        flat_layers.append({
            "name": layer["name"],
            "source_path": layer.get("source_path"),
            "lines": layer.get("lines") or {},
            "in_memory": False,
            "override_time": layer.get("override_time"),
            "_flat": flat,
        })
        if layer["name"] == "base":
            base_flat = flat

    all_keys = set()
    for layer in flat_layers:
        all_keys.update(layer["_flat"].keys())

    in_memory_layers = _compute_in_memory_layers(merged_cfg)
    in_memory_by_name = {l["name"]: l for l in in_memory_layers}

    overrides = []
    by_layer_keys = {layer["name"]: [] for layer in flat_layers}
    for key in sorted(all_keys):
        history = []
        for layer in flat_layers:
            if key not in layer["_flat"]:
                continue
            value = layer["_flat"][key]
            history.append({
                "layer": layer["name"],
                "value": value,
                "source_path": layer.get("source_path"),
                "line_number": layer.get("lines", {}).get(key),
                "override_time": layer.get("override_time"),
                "in_memory": False,
            })
        # Surface state-overlay candidates that may STOMP this key on promote.
        for sl in state_layers:
            if sl["name"].endswith("learning_config_overrides"):
                patch_paths = (sl["value"] or {}).get("_patch_paths") or []
                if key in patch_paths:
                    history.append({
                        "layer": sl["name"],
                        "value": None,
                        "source_path": sl["source_path"],
                        "line_number": None,
                        "override_time": sl["override_time"],
                        "in_memory": True,
                        "state_overlay_only": True,
                    })
            if sl["name"].endswith("symbol_be_trail_live"):
                if (
                    key.startswith("trading.break_even.per_symbol.")
                    or key.startswith("trading.trailing.per_symbol.")
                ):
                    history.append({
                        "layer": sl["name"],
                        "value": None,
                        "source_path": sl["source_path"],
                        "line_number": None,
                        "override_time": sl["override_time"],
                        "in_memory": True,
                        "state_overlay_only": True,
                    })

        winning_layer = None
        winning_value = None
        winning_path = None
        winning_line = None
        winning_time = None
        for entry in reversed(history):
            if not entry.get("in_memory"):
                winning_layer = entry["layer"]
                winning_value = entry["value"]
                winning_path = entry["source_path"]
                winning_line = entry["line_number"]
                winning_time = entry["override_time"]
                break
        if winning_layer is None and history:
            tail = history[-1]
            winning_layer = tail["layer"]
            winning_path = tail["source_path"]
            winning_line = tail["line_number"]
            winning_time = tail["override_time"]

        base_value = base_flat.get(key)
        if base_value is None and key not in base_flat:
            shadowed_status = "introduced_by_overlay"
        elif winning_value is None:
            shadowed_status = "shadowed_to_none"
        else:
            shadowed_status = "shadowed" if winning_value != base_value else "in_sync"

        overrides.append({
            "key": key,
            "value": winning_value,
            "base_value": base_value,
            "shadowed": shadowed_status != "in_sync",
            "shadowed_status": shadowed_status,
            "winning_layer": winning_layer,
            "source_path": winning_path,
            "line_number": winning_line,
            "override_time": winning_time,
            "history": history,
        })
        if winning_layer:
            by_layer_keys.setdefault(winning_layer, []).append(key)

    # Hot conflicts: any key where YAML layers handed 3+ distinct values.
    hot = []
    for o in overrides:
        distinct = []
        seen = set()
        truncated = False
        for h in o["history"]:
            if h.get("in_memory"):
                continue
            try:
                tag = repr(h["value"])
            except Exception:
                tag = str(h["value"])
            if len(tag) > 200:
                tag = tag[:200] + "\u2026"
                truncated = True
            if tag not in seen:
                seen.add(tag)
                distinct.append(tag)
        if len(distinct) >= 3:
            hot.append({
                "key": o["key"],
                "distinct_values": len(distinct),
                "truncated": truncated,
                "history": o["history"],
            })

    active_profile = None
    for layer in flat_layers:
        if layer["name"].startswith("profile:"):
            active_profile = layer["name"].split(":", 1)[1]

    layers_summary = [
        {
            "name": layer["name"],
            "source_path": layer.get("source_path"),
            "in_memory": bool(layer.get("in_memory")),
            "override_time": layer.get("override_time"),
            "key_count": len(layer.get("_flat") or layer.get("value") or {}),
        }
        for layer in flat_layers
    ] + [
        {
            "name": entry["name"],
            "source_path": entry["source_path"],
            "in_memory": True,
            "override_time": None,
            "key_count": 0,
            "note": entry["note"],
            "enabled": entry.get("enabled", True),
        }
        for entry in in_memory_layers
    ] + [
        {
            "name": sl["name"],
            "source_path": sl["source_path"],
            "in_memory": True,
            "override_time": sl["override_time"],
            "key_count": (
                len((sl.get("value") or {}).get("_patch_paths") or [])
                if "_patch_paths" in (sl.get("value") or {}) else 0
            ),
        }
        for sl in state_layers
    ]

    base_size_lines = 0
    base_path = ROOT / "config.yaml"
    if base_path.is_file():
        try:
            base_size_lines = sum(
                1 for ln in base_path.read_text(encoding="utf-8").splitlines() if ln.strip()
            )
        except OSError:
            base_size_lines = 0

    payload = {
        "updated_at": utc_now_iso(),
        "active_profile": active_profile,
        "layers": layers_summary,
        "overrides": overrides,
        "by_layer": {k: v for k, v in by_layer_keys.items() if v},
        "hot_conflicts": hot,
        "summary": {
            "total_keys_tracked": len(all_keys),
            "shadowed_keys": sum(1 for o in overrides if o["shadowed"]),
            "introduced_keys": sum(
                1 for o in overrides if o["shadowed_status"] == "introduced_by_overlay"
            ),
            "hot_conflicts_count": len(hot),
            "base_config_yaml_lines": base_size_lines,
            "cache_ttl_sec": _CLI_OVERRIDES_CACHE_TTL_SEC,
        },
    }
    with _CLI_OVERRIDES_CACHE_LOCK:
        prev = _CLI_OVERRIDES_CACHE.get(cache_key)
        if prev is None or prev["_ts"] <= now or force:
            _CLI_OVERRIDES_CACHE[cache_key] = {"_ts": now, "payload": payload}
    return payload
# === END BLOCK ===
