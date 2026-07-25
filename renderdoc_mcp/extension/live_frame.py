"""Frame-analysis tools that operate on the *live* capture loaded in RenderDoc.

Unlike the standalone MCP server (which opens its own .rdc), this runs inside
qrenderdoc and reads the capture that is currently open in the UI, at the
currently selected event. All RenderDoc work is marshalled onto the replay
thread via ``ctx.Replay().BlockInvoke`` as required by the RenderDoc API.

This module is imported inside qrenderdoc (Python 3.6), so it uses only the
RenderDoc python API and the standard library.
"""

import json

import renderdoc as rd

STAGES = ["Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute"]
STAGE_ABBREV = {
    "Vertex": "VS", "Hull": "HS", "Domain": "DS",
    "Geometry": "GS", "Pixel": "PS", "Compute": "CS",
}


def _rid(resid):
    if resid is None:
        return None
    try:
        if resid == rd.ResourceId.Null():
            return None
    except Exception:
        pass
    return str(resid)


def _enum(value):
    return str(value)


def _counter_int(c):
    """GPUCounter may stringify as a name or as a bare int — always use int id."""
    try:
        return int(c)
    except Exception:
        pass
    try:
        return int(getattr(c, "value", c))
    except Exception:
        s = str(c)
        # e.g. "GPUCounter.EventGPUDuration" / "<GPUCounter.EventGPUDuration: 1>"
        if ":" in s:
            try:
                return int(s.rsplit(":", 1)[-1].strip(" >"))
            except Exception:
                pass
        try:
            return int(s.split(".")[-1])
        except Exception:
            return None


def _counter_label(c, desc=None):
    """Human label for logs: EventGPUDuration (1) when possible."""
    cid = _counter_int(c)
    name = None
    if desc is not None:
        name = getattr(desc, "name", None) or None
    if not name:
        try:
            # Prefer enum member name when binding exposes it.
            for attr in dir(rd.GPUCounter):
                if attr.startswith("_"):
                    continue
                try:
                    if _counter_int(getattr(rd.GPUCounter, attr)) == cid:
                        name = attr
                        break
                except Exception:
                    continue
        except Exception:
            pass
    if name and cid is not None:
        return "%s (%s)" % (name, cid)
    if name:
        return str(name)
    return str(cid if cid is not None else c)


def _index_counters(controller):
    """Map many keys → GPUCounter for robust lookup on Py bindings."""
    available = list(controller.EnumerateCounters())
    by_key = {}
    meta = []
    for c in available:
        desc = controller.DescribeCounter(c)
        cid = _counter_int(c)
        short_enum = _enum(c).split(".")[-1]
        label_name = getattr(desc, "name", "") or ""
        row = {
            "counter": c,
            "id": cid,
            "enum": short_enum,
            "name": label_name,
            "description": getattr(desc, "description", "") or "",
            "unit": _enum(desc.unit),
            "label": _counter_label(c, desc),
        }
        meta.append(row)
        for key in (short_enum, _enum(c), label_name, str(cid) if cid is not None else None):
            if key:
                by_key[str(key)] = c
                by_key[str(key).lower()] = c
        if cid is not None:
            by_key[cid] = c
        # Also index known enum member names that share this int id.
        try:
            for attr in dir(rd.GPUCounter):
                if attr.startswith("_"):
                    continue
                try:
                    if _counter_int(getattr(rd.GPUCounter, attr)) == cid:
                        by_key[attr] = c
                        by_key[attr.lower()] = c
                except Exception:
                    continue
        except Exception:
            pass
    return available, by_key, meta


def _resolve_counter(name, by_key, meta, available):
    """Resolve a user/playbook counter name to a GPUCounter on this capture."""
    aliases = {
        "GPUDuration": "EventGPUDuration",
        "Duration": "EventGPUDuration",
        "EventDuration": "EventGPUDuration",
        "gpu_duration": "EventGPUDuration",
    }
    key = str(name).split(".")[-1]
    key = aliases.get(key, key)

    # Direct map (name / id / enum string).
    c = by_key.get(key)
    if c is None:
        c = by_key.get(key.lower())
    if c is None:
        try:
            c = by_key.get(int(key))
        except Exception:
            pass

    # Enum attribute → match by numeric id against EnumerateCounters().
    if c is None:
        try:
            cand = getattr(rd.GPUCounter, key)
            cid = _counter_int(cand)
            if cid is not None:
                c = by_key.get(cid)
            if c is None and not available:
                c = cand
        except AttributeError:
            pass

    # Substring match on DescribeCounter().name / enum label.
    if c is None:
        kl = key.lower()
        for row in meta:
            hay = "%s %s %s" % (row.get("name") or "", row.get("enum") or "",
                                row.get("description") or "")
            if kl and kl in hay.lower():
                c = row["counter"]
                break

    # Duration aliases: if EventGPUDuration (=1) is present, use it.
    if c is None and key in ("EventGPUDuration", "GPUDuration", "Duration"):
        c = by_key.get(1)
        if c is None:
            try:
                c = by_key.get(_counter_int(rd.GPUCounter.EventGPUDuration))
            except Exception:
                pass

    return c


def _stage_enum(name):
    try:
        return getattr(rd.ShaderStage, name)
    except AttributeError:
        raise ValueError("Unknown shader stage '%s'. Valid: %s" % (name, ", ".join(STAGES)))


def _resource_names(controller):
    names = {}
    for res in controller.GetResources():
        names[str(res.resourceId)] = res.name
    return names


def _action_name(controller, action):
    try:
        return action.GetName(controller.GetStructuredFile())
    except Exception:
        return getattr(action, "customName", "") or ""


def _action_summary(controller, action):
    data = {
        "eventId": int(action.eventId),
        "actionId": int(getattr(action, "actionId", 0)),
        "name": _action_name(controller, action),
        "flags": _enum(action.flags),
    }
    flags = action.flags
    if flags & rd.ActionFlags.Drawcall:
        data["numIndices"] = int(action.numIndices)
        data["numInstances"] = int(action.numInstances)
        data["indexOffset"] = int(action.indexOffset)
        data["baseVertex"] = int(action.baseVertex)
        data["vertexOffset"] = int(action.vertexOffset)
        data["instanceOffset"] = int(action.instanceOffset)
    if flags & rd.ActionFlags.Dispatch:
        data["dispatchDimension"] = [int(x) for x in action.dispatchDimension]
    outputs = [_rid(o) for o in getattr(action, "outputs", [])]
    outputs = [o for o in outputs if o]
    if outputs:
        data["outputs"] = outputs
    depth = _rid(getattr(action, "depthOut", None))
    if depth:
        data["depthOutput"] = depth
    return data


def _build_action_map(controller):
    mapping = {}

    def walk(actions):
        for a in actions:
            mapping[int(a.eventId)] = a
            if a.children:
                walk(a.children)

    walk(controller.GetRootActions())
    return mapping


def _pipeline_summary(controller):
    state = controller.GetPipelineState()
    names = _resource_names(controller)

    def named(resid):
        rid = _rid(resid)
        if rid is None:
            return None
        return {"id": rid, "name": names.get(rid, "")}

    result = {"topology": _enum(state.GetPrimitiveTopology())}

    shaders = {}
    for name in STAGES:
        stage = _stage_enum(name)
        info = named(state.GetShader(stage))
        if info is None:
            continue
        info["entryPoint"] = state.GetShaderEntryPoint(stage)
        shaders[name] = info
    result["shaders"] = shaders

    try:
        targets = []
        for t in state.GetOutputTargets():
            info = named(t.resource)
            if info:
                targets.append(info)
        result["colorTargets"] = targets
    except Exception:
        pass

    try:
        depth = named(state.GetDepthTarget().resource)
        if depth:
            result["depthTarget"] = depth
    except Exception:
        pass

    try:
        viewports = []
        for i in range(8):
            vp = state.GetViewport(i)
            if vp.width == 0 and vp.height == 0:
                continue
            viewports.append({
                "x": vp.x, "y": vp.y, "width": vp.width, "height": vp.height,
                "minDepth": vp.minDepth, "maxDepth": vp.maxDepth,
            })
        if viewports:
            result["viewports"] = viewports
    except Exception:
        pass

    return result


class LiveFrame(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.current_event = 0

    # -- infrastructure ---------------------------------------------------

    def loaded(self):
        try:
            return bool(self.ctx.IsCaptureLoaded())
        except Exception:
            return False

    def run(self, fn):
        box = {}
        err = {}

        def cb(controller):
            try:
                box["v"] = fn(controller)
            except BaseException as exc:  # noqa: BLE001
                err["e"] = exc

        self.ctx.Replay().BlockInvoke(cb)
        if "e" in err:
            raise err["e"]
        return box.get("v")

    def read_at(self, event_id, fn):
        cur = int(self.current_event or 0)
        target = cur if event_id is None else int(event_id)

        def cb(controller):
            controller.SetFrameEvent(target, False)
            try:
                return fn(controller)
            finally:
                if event_id is not None and target != cur:
                    controller.SetFrameEvent(cur, False)

        return self.run(cb)

    def _require_loaded(self):
        if not self.loaded():
            raise RuntimeError("No capture is loaded in RenderDoc.")

    # -- tools ------------------------------------------------------------

    def get_current_frame(self, args):
        self._require_loaded()

        def fn(controller):
            props = controller.GetAPIProperties()
            mapping = _build_action_map(controller)
            action = mapping.get(int(self.current_event))
            data = {
                "api": _enum(props.pipelineType),
                "localRenderer": _enum(props.localRenderer),
                "currentEvent": int(self.current_event),
                "totalActions": len(mapping),
                "action": _action_summary(controller, action) if action else None,
                "pipeline": _pipeline_summary(controller),
            }
            return data

        return json.dumps(self.read_at(None, fn), indent=2)

    def list_actions(self, args):
        self._require_loaded()
        drawcalls_only = bool(args.get("drawcalls_only", False))
        max_depth = int(args.get("max_depth", 0))

        def fn(controller):
            def is_draw(a):
                return bool(a.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch))

            def walk(actions, depth):
                out = []
                for a in actions:
                    include = (not drawcalls_only) or is_draw(a)
                    node = _action_summary(controller, a) if include else None
                    children = []
                    if a.children and (max_depth == 0 or depth < max_depth):
                        children = walk(a.children, depth + 1)
                    if node is not None:
                        if children:
                            node["children"] = children
                        out.append(node)
                    else:
                        out.extend(children)
                return out

            return walk(controller.GetRootActions(), 1)

        return json.dumps(self.run(fn), indent=2)

    def get_action(self, args):
        self._require_loaded()
        event_id = int(args["event_id"])

        def fn(controller):
            mapping = _build_action_map(controller)
            action = mapping.get(event_id)
            if action is None:
                raise RuntimeError("No action found for eventId %d." % event_id)
            data = _action_summary(controller, action)
            sdfile = controller.GetStructuredFile()
            events = []
            for ev in action.events:
                entry = {"eventId": int(ev.eventId), "chunkIndex": int(ev.chunkIndex)}
                try:
                    entry["apiCall"] = sdfile.chunks[ev.chunkIndex].name
                except Exception:
                    pass
                events.append(entry)
            data["events"] = events
            return data

        return json.dumps(self.run(fn), indent=2)

    def get_pipeline_state(self, args):
        self._require_loaded()
        event_id = args.get("event_id")

        def fn(controller):
            summary = _pipeline_summary(controller)
            summary["eventId"] = int(self.current_event if event_id is None else event_id)
            return summary

        return json.dumps(self.read_at(event_id, fn), indent=2)

    def get_shader_disassembly(self, args):
        self._require_loaded()
        stage_name = args["stage"]
        event_id = args.get("event_id")
        target = args.get("target")

        def fn(controller):
            state = controller.GetPipelineState()
            stage = _stage_enum(stage_name)
            refl = state.GetShaderReflection(stage)
            if refl is None:
                raise RuntimeError("No shader bound at stage %s." % stage_name)
            pipe = state.GetGraphicsPipelineObject()
            targets = controller.GetDisassemblyTargets(True)
            chosen = target or (targets[0] if targets else "")
            text = controller.DisassembleShader(pipe, refl, chosen)
            return {
                "stage": stage_name,
                "target": chosen,
                "availableTargets": list(targets),
                "disassembly": text,
            }

        return json.dumps(self.read_at(event_id, fn), indent=2)

    def get_shader_reflection(self, args):
        self._require_loaded()
        stage_name = args["stage"]
        event_id = args.get("event_id")

        def fn(controller):
            state = controller.GetPipelineState()
            stage = _stage_enum(stage_name)
            refl = state.GetShaderReflection(stage)
            if refl is None:
                raise RuntimeError("No shader bound at stage %s." % stage_name)

            def sig(s):
                return {
                    "name": getattr(s, "varName", "") or getattr(s, "semanticName", ""),
                    "semantic": getattr(s, "semanticName", ""),
                    "index": int(getattr(s, "semanticIndex", 0)),
                    "compType": _enum(getattr(s, "compType", "")),
                    "components": int(getattr(s, "compCount", 0)),
                }

            return {
                "stage": stage_name,
                "resourceId": _rid(refl.resourceId),
                "entryPoint": getattr(refl, "entryPoint", ""),
                "inputs": [sig(s) for s in refl.inputSignature],
                "outputs": [sig(s) for s in refl.outputSignature],
                "constantBlocks": [
                    {"name": cb.name, "byteSize": int(getattr(cb, "byteSize", 0)),
                     "variableCount": len(cb.variables)}
                    for cb in refl.constantBlocks
                ],
                "readOnlyResources": [{"name": r.name} for r in refl.readOnlyResources],
                "readWriteResources": [{"name": r.name} for r in refl.readWriteResources],
                "samplers": [{"name": s.name} for s in refl.samplers],
            }

        return json.dumps(self.read_at(event_id, fn), indent=2)

    def list_textures(self, args):
        self._require_loaded()
        name_filter = (args.get("name_filter") or "").lower()

        def fn(controller):
            names = _resource_names(controller)
            out = []
            for tex in controller.GetTextures():
                rid = str(tex.resourceId)
                name = names.get(rid, "")
                if name_filter and name_filter not in name.lower():
                    continue
                fmt = tex.format.Name() if hasattr(tex.format, "Name") else _enum(tex.format)
                out.append({
                    "id": rid, "name": name,
                    "width": int(tex.width), "height": int(tex.height),
                    "depth": int(tex.depth), "arraySize": int(tex.arraysize),
                    "mips": int(tex.mips), "format": _enum(fmt),
                })
            return out

        return json.dumps(self.run(fn), indent=2)

    def list_resources(self, args):
        self._require_loaded()
        name_filter = (args.get("name_filter") or "").lower()

        def fn(controller):
            out = []
            for res in controller.GetResources():
                if name_filter and name_filter not in res.name.lower():
                    continue
                out.append({"id": str(res.resourceId), "name": res.name, "type": _enum(res.type)})
            return out

        return json.dumps(self.run(fn), indent=2)

    def list_counters(self, args):
        self._require_loaded()

        def fn(controller):
            _available, _by_key, meta = _index_counters(controller)
            out = []
            for row in meta:
                c = row["counter"]
                desc = controller.DescribeCounter(c)
                out.append({
                    "counter": row["label"],
                    "id": row["id"],
                    "name": desc.name,
                    "description": desc.description,
                    "unit": _enum(desc.unit),
                    "resultType": _enum(desc.resultType),
                    "resultByteWidth": int(desc.resultByteWidth),
                })
            return out

        return json.dumps(self.run(fn), indent=2)

    def pick_duration_counter(self, args):
        """Return the best available GPU duration counter for this capture.

        RenderDoc's enum is ``EventGPUDuration`` (=1). Some Python bindings
        stringify counters as bare ints, so we match by id/name robustly.
        """
        self._require_loaded()

        def fn(controller):
            _available, by_key, meta = _index_counters(controller)
            available = [{
                "counter": row["label"],
                "id": row["id"],
                "short": row["enum"],
                "name": row["name"],
                "description": row["description"],
                "unit": row["unit"],
            } for row in meta]
            preferred = ("EventGPUDuration", "GPUDuration", "Duration")
            chosen = None
            for short in preferred:
                c = _resolve_counter(short, by_key, meta, _available)
                if c is not None:
                    for row in meta:
                        if row["counter"] == c or row["id"] == _counter_int(c):
                            chosen = {
                                "counter": row["label"],
                                "id": row["id"],
                                "short": "EventGPUDuration" if row["id"] == 1 else row["enum"],
                                "name": row["name"],
                                "description": row["description"],
                                "unit": row["unit"],
                            }
                            break
                if chosen is not None:
                    break
            return {"chosen": chosen, "available": available}

        return json.dumps(self.run(fn), indent=2)

    def fetch_counters(self, args):
        self._require_loaded()
        counters = args.get("counters") or []
        event_ids = args.get("event_ids")

        def fn(controller):
            available, by_key, meta = _index_counters(controller)
            wanted_names = list(counters) if counters else ["EventGPUDuration"]

            counter_enums = []
            for name in wanted_names:
                c = _resolve_counter(name, by_key, meta, available)
                if c is None:
                    avail_names = sorted(set(
                        row["label"] for row in meta)) or ["(none)"]
                    raise RuntimeError(
                        "Unknown or unavailable counter '%s'. "
                        "Available on this capture: %s" % (
                            name, ", ".join(avail_names)))
                if c not in counter_enums:
                    counter_enums.append(c)

            descs = {}
            for c in counter_enums:
                descs[c] = controller.DescribeCounter(c)
            results = controller.FetchCounters(counter_enums)
            wanted = set(int(e) for e in event_ids) if event_ids else None
            out = []
            for r in results:
                if wanted is not None and int(r.eventId) not in wanted:
                    continue
                # CounterResult.counter may be int; match desc by id.
                desc = descs.get(r.counter)
                if desc is None:
                    rid = _counter_int(r.counter)
                    for c, d in descs.items():
                        if _counter_int(c) == rid:
                            desc = d
                            break
                if desc is not None and desc.resultType == rd.CompType.Float:
                    value = float(r.value.d) if desc.resultByteWidth == 8 else float(r.value.f)
                elif desc is not None and desc.resultByteWidth == 8:
                    value = int(r.value.u64)
                else:
                    value = int(r.value.u32)
                out.append({
                    "eventId": int(r.eventId),
                    "counter": _counter_label(r.counter, desc),
                    "value": value,
                })
            return out

        return json.dumps(self.run(fn), indent=2)

    def get_event_chunk(self, args):
        self._require_loaded()
        event_id = int(args["event_id"])

        def fn(controller):
            mapping = _build_action_map(controller)
            action = mapping.get(event_id)
            if action is None:
                raise RuntimeError("No action found for eventId %d." % event_id)
            sdfile = controller.GetStructuredFile()
            chunk = None
            for ev in action.events:
                if int(ev.eventId) == event_id:
                    chunk = sdfile.chunks[ev.chunkIndex]
                    break
            if chunk is None and action.events:
                chunk = sdfile.chunks[action.events[-1].chunkIndex]
            if chunk is None:
                raise RuntimeError("No chunk found for eventId %d." % event_id)

            def serialise(obj, depth=0):
                node = getattr(obj, "data", obj)
                children = list(node.children) if hasattr(node, "children") and node.children else []
                if children and depth < 6:
                    return dict((c.name, serialise(c, depth + 1)) for c in children)
                try:
                    return obj.AsString() if hasattr(obj, "AsString") else str(obj)
                except Exception:
                    return str(obj)

            params = {}
            for child in chunk.data.children:
                params[child.name] = serialise(child)
            return {"eventId": event_id, "apiCall": chunk.name, "parameters": params}

        return json.dumps(self.run(fn), indent=2)


    def _find_resource_id(self, controller, resource_id):
        wanted = str(resource_id).strip()
        if not wanted.startswith("ResourceId::"):
            wanted = "ResourceId::%s" % wanted
        for res in controller.GetResources():
            if str(res.resourceId) == wanted:
                return res.resourceId
        for tex in controller.GetTextures():
            if str(tex.resourceId) == wanted:
                return tex.resourceId
        for buf in controller.GetBuffers():
            if str(buf.resourceId) == wanted:
                return buf.resourceId
        raise RuntimeError("Resource id '%s' not found." % resource_id)

    def get_resource_usage(self, args):
        self._require_loaded()
        resource_id = args["resource_id"]

        def fn(controller):
            resid = self._find_resource_id(controller, resource_id)
            names = _resource_names(controller)
            usages = []
            for u in controller.GetUsage(resid):
                usages.append({"eventId": int(u.eventId), "usage": _enum(u.usage)})
            return {
                "resourceId": str(resid),
                "name": names.get(str(resid), ""),
                "usages": usages,
                "count": len(usages),
            }

        return json.dumps(self.run(fn), indent=2)

    def pick_pixel(self, args):
        self._require_loaded()
        resource_id = args["resource_id"]
        x = int(args["x"])
        y = int(args["y"])
        event_id = args.get("event_id")
        mip = int(args.get("mip") or 0)
        slice_index = int(args.get("slice_index") or 0)
        sample = int(args.get("sample") or 0)
        type_cast = str(args.get("type_cast") or "Typeless")

        def fn(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), False)
            resid = self._find_resource_id(controller, resource_id)
            sub = rd.Subresource(mip, slice_index, sample)
            cast = getattr(rd.CompType, type_cast)
            val = controller.PickPixel(resid, x, y, sub, cast)
            out = {"resourceId": str(resid), "x": x, "y": y, "value": {}}
            try:
                out["value"]["float"] = [float(v) for v in val.floatValue]
            except Exception:
                pass
            return out

        return json.dumps(self.run(fn), indent=2)

    def get_pixel_history(self, args):
        self._require_loaded()
        resource_id = args["resource_id"]
        x = int(args["x"])
        y = int(args["y"])
        event_id = args.get("event_id")
        mip = int(args.get("mip") or 0)
        slice_index = int(args.get("slice_index") or 0)
        sample = int(args.get("sample") or 0)
        type_cast = str(args.get("type_cast") or "Typeless")
        max_mods = int(args.get("max_mods") or 100)

        def fn(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), False)
            resid = self._find_resource_id(controller, resource_id)
            sub = rd.Subresource(mip, slice_index, sample)
            cast = getattr(rd.CompType, type_cast)
            mods = list(controller.PixelHistory(resid, x, y, sub, cast))
            shown = mods[: max(1, max_mods)]
            out_mods = []
            for m in shown:
                item = {
                    "eventId": int(m.eventId),
                    "primitiveID": int(getattr(m, "primitiveID", 0)),
                }
                try:
                    item["passed"] = bool(m.Passed())
                except Exception:
                    item["passed"] = None
                try:
                    item["postMod"] = {
                        "col": {"float": [float(v) for v in m.postMod.col.floatValue]}
                    }
                except Exception:
                    pass
                out_mods.append(item)
            return {
                "resourceId": str(resid),
                "x": x,
                "y": y,
                "totalMods": len(mods),
                "returned": len(shown),
                "truncated": len(mods) > len(shown),
                "modifications": out_mods,
            }

        return json.dumps(self.run(fn), indent=2)

    def get_debug_messages(self, args):
        self._require_loaded()

        def fn(controller):
            out = []
            for msg in controller.GetDebugMessages():
                out.append({
                    "eventId": int(getattr(msg, "eventId", 0)),
                    "category": _enum(getattr(msg, "category", "")),
                    "severity": _enum(getattr(msg, "severity", "")),
                    "description": str(getattr(msg, "description", "")),
                })
            return out

        return json.dumps(self.run(fn), indent=2)

    def get_descriptor_access(self, args):
        self._require_loaded()
        event_id = args.get("event_id")

        def fn(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), False)
            access = []
            for a in controller.GetDescriptorAccess():
                access.append({
                    "stage": _enum(getattr(a, "stage", "")),
                    "type": _enum(getattr(a, "type", "")),
                    "index": int(getattr(a, "index", 0)),
                    "arrayElement": int(getattr(a, "arrayElement", 0)),
                    "descriptorStore": _rid(getattr(a, "descriptorStore", None)),
                    "byteOffset": int(getattr(a, "byteOffset", 0)),
                })
            return {
                "eventId": int(event_id) if event_id is not None else None,
                "access": access,
                "count": len(access),
            }

        return json.dumps(self.run(fn), indent=2)
