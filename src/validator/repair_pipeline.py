import logging
from collections import defaultdict
from typing import List, Dict, Set, Tuple
from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.schemas.logical_plan import SketchPlan, LogicalPlan, SemanticEntity, SketchRelationship

logger = logging.getLogger(__name__)

WHITE, GRAY, BLACK = 0, 1, 2

# Severity classification for action_log entries, cheapest (no real information lost)
# to most severe (a whole node destroyed, or structure/semantics invented that the LLM
# never proposed). This is the measurement infrastructure for deciding whether Phase
# 2.fb (a capped, constraint-aware LLM regenerate triggered on lossy repair) is worth
# building at all - per the "ĐO" step of the Phase 2 repair-layer audit: only build it
# if live-run metrics show `invent_or_destroy`/`drop_edge` actions still fire
# meaningfully often AFTER Tầng 0 (kind-aware schema) + Tầng 1 (prompt constraints) are
# in place. Deliberately NOT built yet - measure first, build only if the number says so.
#   normalize          - re-typed/re-labeled an edge, no information destroyed
#   retype              - a relationship's TYPE changed, but the edge itself survives
#   drop_edge           - an edge is dropped or forcibly rerouted; no entity lost, no kind invented
#   invent_or_destroy   - a node was destroyed, or code invented structure/kind the LLM never proposed
LOSSINESS = {
    "2.0_dedupe": "normalize",
    "2.0_dangling": "normalize",
    "2.0_normalization": "normalize",
    "2.0c_duplicate_edge": "normalize",
    "2.13_duplicate_edge_cleanup": "normalize",
    "2.1a_class_extends_interface": "normalize",
    "2.1b_interface_extends_class": "normalize",
    "2.10_implements_cleanup": "normalize",
    "2.10c_class_extends_became_interface": "normalize",
    "2.10c_interface_extends_became_class": "normalize",
    "2.7_aggregation_fallback": "retype",
    "2.1c_cycle_detection": "drop_edge",
    "2.1d_interface_cycle_detection": "drop_edge",
    "2.2_conflict_resolution": "drop_edge",
    "2.3_depth_exceeded": "drop_edge",
    "2.7_composition_cycle": "drop_edge",
    "2.10_interface_state": "drop_edge",
    "2.10c_orphaned_interface_extends": "drop_edge",
    "2.14_interface_outgoing_edge_sweep": "drop_edge",
    "2.14_implements_target_sweep": "normalize",
    "2.15_has_a_collision_cleanup": "drop_edge",
    "2.16_inherited_has_a_collision_cleanup": "drop_edge",
    "2.5_excess_classes": "invent_or_destroy",
    "2.6_derive_interface": "invent_or_destroy",
    "2.6b_derive_abstract": "invent_or_destroy",
    "2.8_isolated_core": "invent_or_destroy",
    "2.8_isolated_non_core": "invent_or_destroy",
    "2.11_orphan_interface": "invent_or_destroy",
    "2.12_post_cleanup_isolation": "invent_or_destroy",
}


class StructuralRepairPipeline:
    def __init__(self, preset: BlueprintPreset, domain: DomainConfig):
        self.preset = preset
        self.domain = domain
        self.action_log = []
        self.converged = True

    def log_action(self, step: str, node: str, detail: str, action: str):
        self.action_log.append({
            "step": step,
            "node": node,
            "detail": detail,
            "action": action
        })
        logger.info(f"[{step}] Node '{node}': {detail} -> {action}")

    def lossiness_summary(self) -> Dict[str, int]:
        """Count this run's action_log entries by severity (see LOSSINESS above).
        Cheap, deterministic, no LLM involved - safe to call after every repair()."""
        counts = {"normalize": 0, "retype": 0, "drop_edge": 0, "invent_or_destroy": 0, "unknown": 0}
        for entry in self.action_log:
            counts[LOSSINESS.get(entry["step"], "unknown")] += 1
        return counts

    def repair(self, sketch: SketchPlan) -> SketchPlan:
        # Deepcopy to prevent mutating the Pass 1 snapshot
        sketch = sketch.model_copy(deep=True)

        # 2.0 Normalization: Dedupe entities (case-insensitive)
        entities = {}
        name_to_canonical = {}
        for e in sketch.entities:
            key = e.name.lower()
            if key in entities:
                self.log_action("2.0_dedupe", e.name, f"duplicate entity name (case-insensitive) with {entities[key].name}", "dropped duplicate")
            else:
                entities[key] = e
            name_to_canonical[e.name] = entities[key].name

        valid_entity_names = {e.name for e in entities.values()}

        # 2.0b Inheritance lives on the entity itself now (SketchEntity.extends /
        # extends_interfaces - see the Tầng 0 schema change), not in `relationships`.
        # Two SEPARATE well-formedness domains, never intermixed per JLS: a
        # class/abstract-class hierarchy is a single-parent tree (class_parent_map),
        # an interface hierarchy is a multi-parent DAG (iface_parent_map). Which map an
        # entity's declared parent(s) go into is decided by ITS OWN is_interface at
        # this point in time (before 2.6/2.6b may still derive a different kind further
        # down - see the 2.10c consistency pass near the end for how that's reconciled).
        class_parent_map: Dict[str, str] = {}
        iface_parent_map: Dict[str, List[str]] = {}

        for e in entities.values():
            if e.is_interface:
                parents = list(dict.fromkeys(
                    [name_to_canonical.get(p, p) for p in e.extends_interfaces]
                    + ([name_to_canonical.get(e.extends, e.extends)] if e.extends else [])
                ))
                parents = [p for p in parents if p in valid_entity_names and p != e.name]
                if parents:
                    iface_parent_map[e.name] = parents
            else:
                parent = name_to_canonical.get(e.extends, e.extends) if e.extends else None
                if parent and parent in valid_entity_names and parent != e.name:
                    class_parent_map[e.name] = parent

        # Split structural edges, remove self-loops, and prune dangling references.
        # No "inheritance" type exists here anymore - SketchRelationship.type's Literal
        # no longer includes it, so this list is guaranteed pure has-a/implements edges.
        structural_edges: List[SketchRelationship] = []

        seen_edges: Set[Tuple[str, str, str]] = set()
        for r in sketch.relationships:
            r.from_entity = name_to_canonical.get(r.from_entity, r.from_entity)
            r.to_entity = name_to_canonical.get(r.to_entity, r.to_entity)

            if r.from_entity not in valid_entity_names or r.to_entity not in valid_entity_names:
                self.log_action("2.0_dangling", r.from_entity, f"edge points to missing entity ({r.from_entity} -> {r.to_entity})", "dropped edge")
                continue

            if r.from_entity == r.to_entity:
                self.log_action("2.0_normalization", r.from_entity, "self-loop detected", "dropped edge")
                continue

            # 2.0c Dedupe exact-duplicate relationships (same from/to/type) - found live
            # via fuzzing: e.g. two identical `composition` edges for the same pair
            # render as two identical fields/constructor params, a real javac
            # "variable already defined" error. Pre-existing gap (2.0 only ever
            # deduped entities, never relationships), found independently while
            # auditing this rewrite.
            key = (r.from_entity, r.to_entity, r.type)
            if key in seen_edges:
                self.log_action("2.0c_duplicate_edge", r.from_entity, f"duplicate {r.type} edge to {r.to_entity}", "dropped duplicate")
                continue
            seen_edges.add(key)

            structural_edges.append(r)

        # 2.9 Convergence Guard
        converged = False
        iteration = 0
        while not converged and iteration < 10:
            iteration += 1
            converged = True

            # 2.1a Class/abstract-class extends target must not be an interface (JLS:
            # a class can never extend an interface). Only catches an EXPLICIT
            # mismatch from Phase 1 here - a kind derived later by 2.6/2.6b is handled
            # by the 2.10c consistency pass after the loop, once kind has settled.
            for child in list(class_parent_map.keys()):
                parent = class_parent_map[child]
                if entities[parent.lower()].is_interface:
                    self.log_action("2.1a_class_extends_interface", child, f"target {parent} is an interface", "downgraded to implements")
                    del class_parent_map[child]
                    structural_edges.append(SketchRelationship(from_entity=child, to_entity=parent, type="implements"))
                    converged = False

            # 2.1b Interface extends target must itself be an interface (JLS: an
            # interface can never extend a class). Same explicit-mismatch-only scope
            # as 2.1a above.
            for child in list(iface_parent_map.keys()):
                kept = []
                for p in iface_parent_map[child]:
                    if entities[p.lower()].is_interface:
                        kept.append(p)
                    else:
                        self.log_action("2.1b_interface_extends_class", child, f"target {p} is not an interface", "downgraded to implements")
                        structural_edges.append(SketchRelationship(from_entity=child, to_entity=p, type="implements"))
                        converged = False
                if kept:
                    iface_parent_map[child] = kept
                else:
                    del iface_parent_map[child]

            # 2.1c Break cycles in the class hierarchy (single-parent tree)
            color = {n: WHITE for n in valid_entity_names}
            broken_edges = set()

            def dfs_inherit(node):
                color[node] = GRAY
                parent = class_parent_map.get(node)
                if parent:
                    if color.get(parent, WHITE) == GRAY:
                        self.log_action("2.1c_cycle_detection", node, f"inheritance cycle detected involving {parent}", "broke edge, downgraded to association")
                        broken_edges.add(node)
                    elif color.get(parent, WHITE) == WHITE:
                        dfs_inherit(parent)
                color[node] = BLACK

            for n in list(class_parent_map):
                if color.get(n, WHITE) == WHITE:
                    dfs_inherit(n)

            for child in broken_edges:
                parent = class_parent_map.pop(child)
                structural_edges.append(SketchRelationship(from_entity=child, to_entity=parent, type="association"))
                converged = False

            # 2.1d Break cycles in the interface hierarchy (multi-parent DAG) - same
            # WHITE/GRAY/BLACK scheme, generalized to multiple children per node. A
            # diamond (A extends B,C; B extends D; C extends D) is NOT a cycle and
            # must survive - standard DFS handles this correctly (D visited once,
            # marked BLACK on the first path, later paths just see BLACK and stop).
            iface_color = {n: WHITE for n in valid_entity_names}
            broken_iface_edges = set()

            def dfs_iface(node):
                iface_color[node] = GRAY
                for parent in list(iface_parent_map.get(node, [])):
                    if iface_color.get(parent, WHITE) == GRAY:
                        self.log_action("2.1d_interface_cycle_detection", node, f"interface inheritance cycle detected involving {parent}", "broke edge")
                        broken_iface_edges.add((node, parent))
                    elif iface_color.get(parent, WHITE) == WHITE:
                        dfs_iface(parent)
                iface_color[node] = BLACK

            for n in list(iface_parent_map):
                if iface_color.get(n, WHITE) == WHITE:
                    dfs_iface(n)

            for child, parent in broken_iface_edges:
                if child in iface_parent_map and parent in iface_parent_map[child]:
                    iface_parent_map[child] = [p for p in iface_parent_map[child] if p != parent]
                    if not iface_parent_map[child]:
                        del iface_parent_map[child]
                    converged = False

            # 2.3 Depth Constraint (class hierarchy only - interfaces have no
            # pedagogical depth notion in the current preset schema)
            max_depth = self.preset.oop.inheritance.max_depth if self.preset.oop and self.preset.oop.inheritance else None
            if max_depth is not None:
                depth_map = {}
                def get_depth(node):
                    if node not in depth_map:
                        p = class_parent_map.get(node)
                        if p:
                            depth_map[node] = get_depth(p) + 1
                        else:
                            depth_map[node] = 0
                    return depth_map[node]

                for n in list(valid_entity_names):
                    d = get_depth(n)
                    if d > max_depth:
                        path = []
                        temp = n
                        while temp:
                            path.append(temp)
                            temp = class_parent_map.get(temp)
                        path.reverse()

                        if len(path) > max_depth:
                            new_parent = path[max_depth - 1]
                            self.log_action("2.3_depth_exceeded", n, f"depth={d} > max={max_depth}", f"reattached to {new_parent}")
                            class_parent_map[n] = new_parent
                            depth_map.clear()
                            converged = False

            # Helper for transitive ancestor checking (class hierarchy only - has-a
            # edges never conflict with interface extends since interfaces can't hold
            # state, see 2.10_interface_state below)
            def ancestors(node: str) -> set:
                ancs = set()
                curr = class_parent_map.get(node)
                while curr:
                    ancs.add(curr)
                    curr = class_parent_map.get(curr)
                return ancs

            # 2.2 Conflict Resolution
            valid_structural = []
            for r in structural_edges:
                u, v = r.from_entity, r.to_entity
                u_ancestors = ancestors(u)
                v_ancestors = ancestors(v)

                if v in u_ancestors or u in v_ancestors:
                    self.log_action("2.2_conflict_resolution", u, f"structural edge conflicts with transitive inheritance involving {v}", "dropped structural edge")
                    converged = False
                    continue
                valid_structural.append(r)

            structural_edges = valid_structural

            # 2.7 Aggregation Fallback
            agg_enabled = self.preset.oop.aggregation.enabled if self.preset.oop and self.preset.oop.aggregation else False
            if not agg_enabled:
                for r in structural_edges:
                    if r.type == "aggregation":
                        self.log_action("2.7_aggregation_fallback", r.from_entity, "aggregation disabled in preset", "converted to composition")
                        r.type = "composition"
                        converged = False

            # 2.7 Acyclic Composition Check
            # A owns B, B cannot own A transitively.
            comp_adj = defaultdict(list)
            for r in structural_edges:
                if r.type == "composition":
                    comp_adj[r.from_entity].append(r)

            comp_color = {n: WHITE for n in valid_entity_names}
            broken_comp_edges = set()

            def dfs_comp(node):
                comp_color[node] = GRAY
                for r in comp_adj[node]:
                    child = r.to_entity
                    if comp_color.get(child, WHITE) == GRAY:
                        self.log_action("2.7_composition_cycle", node, f"composition cycle detected involving {child}", "downgraded to association")
                        broken_comp_edges.add((node, child))
                    elif comp_color.get(child, WHITE) == WHITE:
                        dfs_comp(child)
                comp_color[node] = BLACK

            for n in valid_entity_names:
                if comp_color[n] == WHITE:
                    dfs_comp(n)

            # Downgrade cyclic compositions
            for r in structural_edges:
                if r.type == "composition" and (r.from_entity, r.to_entity) in broken_comp_edges:
                    r.type = "association"
                    converged = False

            # 2.5 Excess Classes
            max_classes = self.preset.structure.classes.max
            if len(valid_entity_names) > max_classes:
                def score(node):
                    s = 0
                    if entities[node.lower()].kind != "core": s += 10
                    deg = sum(1 for r in structural_edges if r.from_entity == node or r.to_entity == node)
                    deg += 1 if class_parent_map.get(node) else 0
                    deg += sum(1 for p in class_parent_map.values() if p == node)
                    deg += len(iface_parent_map.get(node, []))
                    deg += sum(1 for parents in iface_parent_map.values() if node in parents)
                    if deg == 0: s += 50
                    elif deg == 1: s += 5
                    is_parent = (node in class_parent_map.values()) or any(node in parents for parents in iface_parent_map.values())
                    if not is_parent: s += 20
                    return s

                # Drop the whole excess in this iteration, not one node at a time - the
                # convergence loop is capped at 10 iterations, so a single-node-per-round
                # policy silently leaves the graph over max_classes whenever more than 10
                # classes need to go (verified live: max=5, 20 input entities -> 10
                # survived, not 5). Scores are computed once against the pre-drop graph;
                # since a drop only ever loosens (never tightens) another node's score
                # (removes edges, never adds), taking the worst-scoring excess_count nodes
                # from that one ranking is equivalent to recomputing after each drop.
                sorted_nodes = sorted(valid_entity_names, key=lambda n: score(n), reverse=True)
                excess_count = len(valid_entity_names) - max_classes
                nodes_to_drop = set(sorted_nodes[:excess_count])
                for node_to_drop in nodes_to_drop:
                    self.log_action("2.5_excess_classes", node_to_drop, f"total classes {len(valid_entity_names)} > {max_classes}", "dropped entity")
                    valid_entity_names.remove(node_to_drop)
                    del entities[node_to_drop.lower()]

                structural_edges = [r for r in structural_edges if r.from_entity not in nodes_to_drop and r.to_entity not in nodes_to_drop]
                class_parent_map = {c: p for c, p in class_parent_map.items() if c not in nodes_to_drop and p not in nodes_to_drop}
                iface_parent_map = {c: [p for p in ps if p not in nodes_to_drop] for c, ps in iface_parent_map.items() if c not in nodes_to_drop}
                iface_parent_map = {c: ps for c, ps in iface_parent_map.items() if ps}

                converged = False

            # 2.8 Disconnected component check
            adj = defaultdict(set)
            for r in structural_edges:
                adj[r.from_entity].add(r.to_entity)
                adj[r.to_entity].add(r.from_entity)
            for child, parent in class_parent_map.items():
                adj[child].add(parent)
                adj[parent].add(child)
            for child, parents in iface_parent_map.items():
                for parent in parents:
                    adj[child].add(parent)
                    adj[parent].add(child)

            for node in list(valid_entity_names):
                if not adj[node]:
                    if entities[node.lower()].kind == "core":
                        other_cores = [n for n in valid_entity_names if n != node and entities[n.lower()].kind == "core"]
                        target = other_cores[0] if other_cores else (list(valid_entity_names - {node})[0] if valid_entity_names - {node} else None)
                        if target:
                            self.log_action("2.8_isolated_core", node, "core node is isolated", f"added association to {target}")
                            structural_edges.append(SketchRelationship(from_entity=node, to_entity=target, type="association"))
                            adj[node].add(target)
                            adj[target].add(node)
                            converged = False
                    else:
                        self.log_action("2.8_isolated_non_core", node, "non-core node is isolated", "dropped entity")
                        valid_entity_names.remove(node)
                        del entities[node.lower()]
                        converged = False

        # Exposed as a real attribute (not just a log line) so a caller can check
        # `repair_engine.converged` and decide to fail/retry/flag - EDGE-1 (excess
        # classes overflowing the 10-iteration cap) used to be visible only via this
        # print, which nothing downstream ever read.
        self.converged = converged
        if not converged:
            logger.warning(f"Repair pipeline did not converge after {iteration} iterations")
            print(f"[WARNING] Repair pipeline did not converge after {iteration} iterations")

        # 2.6 Interfaces & 2.6b Abstractions
        # Only derives a kind for an entity the LLM left unset (is_interface=False and
        # is_abstract=False at this point) - if Phase 1 already claimed a kind
        # explicitly, that claim is trusted, not overridden. children_count merges
        # both hierarchies since either can make a node "look like" a natural
        # interface/abstract base.
        children_count = defaultdict(int)
        for p in class_parent_map.values():
            children_count[p] += 1
        for parents in iface_parent_map.values():
            for p in parents:
                children_count[p] += 1

        implements_target_count = defaultdict(int)
        for r in structural_edges:
            if r.type == "implements":
                implements_target_count[r.to_entity] += 1

        interface_enabled = self.preset.oop.interface.enabled if self.preset.oop and self.preset.oop.interface else False
        abstraction_enabled = self.preset.oop.abstraction.enabled if self.preset.oop and self.preset.oop.abstraction else False

        for node in valid_entity_names:
            e = entities[node.lower()]
            if e.is_interface or e.is_abstract:
                continue  # LLM already claimed a kind explicitly - heuristic only fills in the unset case

            count = children_count.get(node, 0)
            impl_count = implements_target_count.get(node, 0)

            if count >= 1 or impl_count >= 1:
                is_semantic_interface = (e.kind == "supporting") or e.name.endswith("able") or e.name.endswith("ible")
                note_lower = e.note.lower() if e.note else ""
                has_state_keywords = any(kw in note_lower for kw in ["properties", "balance", "attributes", "fields", "state", "core properties"])
                is_explicitly_abstract = "abstract" in note_lower or "base" in note_lower
                out_edges_count = sum(1 for r in structural_edges if r.from_entity == node and r.type in ["composition", "aggregation", "association"])

                qualifies_as_interface = (
                    (interface_enabled and count >= 2 and is_semantic_interface and not has_state_keywords and out_edges_count == 0)
                    or (interface_enabled and impl_count >= 1 and is_semantic_interface and not has_state_keywords and out_edges_count == 0)
                )

                if qualifies_as_interface:
                    self.log_action("2.6_derive_interface", node, f"semantic signals + {count} children + {impl_count} implements-targets", "marked as interface")
                    e.is_interface = True
                elif abstraction_enabled:
                    if count >= 2 or is_explicitly_abstract:
                        self.log_action("2.6b_derive_abstract", node, f"node has {count} children/abstract semantic", "marked as abstract class")
                        e.is_abstract = True

        # 2.10 Post-Interface Cleanup
        final_structural_edges = []
        for r in structural_edges:
            if r.type == "implements":
                if not entities[r.to_entity.lower()].is_interface:
                    self.log_action("2.10_implements_cleanup", r.from_entity, f"target {r.to_entity} is not an interface", "downgraded implements to association")
                    r.type = "association"

            if r.from_entity in valid_entity_names:
                e = entities[r.from_entity.lower()]
                # An interface can never legitimately have an OUTGOING `implements`
                # edge (JLS: interfaces implement nothing, they only extend other
                # interfaces via extends_interfaces) - found live via fuzzing: this
                # list previously omitted "implements", so `interface A implements B`
                # rendered and failed javac with "'{' expected" whenever a relationship
                # (not extends_interfaces) declared it that way, e.g. after 2.1a/2.1b
                # downgraded a mismatched extends into an `implements` edge.
                if e.is_interface and r.type in ["composition", "aggregation", "association", "implements"]:
                    self.log_action("2.10_interface_state", r.from_entity, f"interface cannot hold state or implement ({r.type} to {r.to_entity})", "dropped edge")
                    continue

            final_structural_edges.append(r)
        structural_edges = final_structural_edges

        # 2.10c Kind-consistency re-check (EDGE-2-style bug class): 2.6/2.6b above can
        # derive a NEW kind for a node AFTER class_parent_map/iface_parent_map were
        # already built from its ORIGINAL (pre-derivation) kind. A class quietly
        # pointing at a target that just became an interface (or an interface pointing
        # at a target that's still a plain class) must be caught here - the 2.1a/2.1b
        # checks inside the loop only ever saw the pre-derivation snapshot.
        final_class_parent_map = {}
        for child, parent in class_parent_map.items():
            if entities[parent.lower()].is_interface:
                self.log_action("2.10c_class_extends_became_interface", child, f"target {parent} was derived as an interface after repair", "downgraded to implements")
                structural_edges.append(SketchRelationship(from_entity=child, to_entity=parent, type="implements"))
                continue
            final_class_parent_map[child] = parent
        class_parent_map = final_class_parent_map

        final_iface_parent_map = {}
        for child, parents in iface_parent_map.items():
            if not entities[child.lower()].is_interface:
                for p in parents:
                    self.log_action("2.10c_orphaned_interface_extends", child, f"{child} is not (or no longer) an interface", "dropped inheritance edge")
                continue
            kept = [p for p in parents if entities[p.lower()].is_interface]
            for p in [p for p in parents if p not in kept]:
                self.log_action("2.10c_interface_extends_became_class", child, f"target {p} is not an interface", "downgraded to implements")
                structural_edges.append(SketchRelationship(from_entity=child, to_entity=p, type="implements"))
            if kept:
                final_iface_parent_map[child] = kept
        iface_parent_map = final_iface_parent_map

        # 2.11 Orphan interface demotion
        # An interface with zero implementers and zero inheritors (whether it was derived by
        # 2.6 or set directly by the LLM in Phase 1) is structurally valid Java but pedagogically
        # meaningless - nothing in the generated assignment shows how to use it. Demote it back
        # to a concrete class rather than inventing a fake implementer.
        final_implements_count = defaultdict(int)
        for r in structural_edges:
            if r.type == "implements":
                final_implements_count[r.to_entity] += 1
        final_children_count = defaultdict(int)
        for p in class_parent_map.values():
            final_children_count[p] += 1
        for parents in iface_parent_map.values():
            for p in parents:
                final_children_count[p] += 1

        for node in valid_entity_names:
            e = entities[node.lower()]
            if e.is_interface:
                has_implementer = final_implements_count.get(node, 0) >= 1 or final_children_count.get(node, 0) >= 1
                if not has_implementer:
                    self.log_action("2.11_orphan_interface", node, "interface has zero implementers/inheritors after repair", "demoted to concrete class")
                    e.is_interface = False

        # 2.12 Post-cleanup isolation re-check (EDGE-2 fix)
        # 2.10/2.10c/2.11 above run AFTER the convergence loop and can drop a node's
        # only remaining edge (e.g. 2.10_interface_state dropping an interface's has-a
        # edge to some entity whose ONLY edge that was) - nothing re-ran 2.8's isolation
        # check afterward, so that entity silently survived in the output as a fully
        # disconnected node. Verified live: interface holding a state edge to a
        # `Lonely` entity with no other edges left `Lonely` orphaned after repair with
        # no error. Same policy as 2.8: reattach an isolated core entity, drop an
        # isolated non-core one. Only needs one pass (not a re-run of the whole loop):
        # attaching a plain association or dropping a childless node can't reintroduce
        # any violation the earlier rules (inheritance forest, depth, composition
        # acyclicity, max_classes) care about.
        final_adj = defaultdict(set)
        for r in structural_edges:
            final_adj[r.from_entity].add(r.to_entity)
            final_adj[r.to_entity].add(r.from_entity)
        for child, parent in class_parent_map.items():
            final_adj[child].add(parent)
            final_adj[parent].add(child)
        for child, parents in iface_parent_map.items():
            for parent in parents:
                final_adj[child].add(parent)
                final_adj[parent].add(child)

        for node in list(valid_entity_names):
            if not final_adj[node]:
                if entities[node.lower()].kind == "core":
                    other_cores = [n for n in valid_entity_names if n != node and entities[n.lower()].kind == "core"]
                    target = other_cores[0] if other_cores else (list(valid_entity_names - {node})[0] if valid_entity_names - {node} else None)
                    if target:
                        self.log_action("2.12_post_cleanup_isolation", node, "core node isolated by post-loop cleanup rules", f"added association to {target}")
                        structural_edges.append(SketchRelationship(from_entity=node, to_entity=target, type="association"))
                else:
                    self.log_action("2.12_post_cleanup_isolation", node, "non-core node isolated by post-loop cleanup rules", "dropped entity")
                    valid_entity_names.remove(node)
                    del entities[node.lower()]

        # Write the final, repaired inheritance state back onto each surviving entity.
        for e in entities.values():
            if e.is_interface:
                e.extends = None
                e.extends_interfaces = iface_parent_map.get(e.name, [])
            else:
                e.extends = class_parent_map.get(e.name)
                e.extends_interfaces = []

        # 2.13 Final dedupe pass. 2.0c above only dedupes what the LLM originally sent -
        # several rules append a NEW structural edge as part of downgrading a mismatch
        # (2.1a, 2.1b, 2.10c), and none of them check whether an edge for that exact
        # (from, to, type) already exists first. Found live via fuzzing: a class with
        # both an explicit `extends` pointing at an interface AND a separate, redundant
        # `implements` relationship to the SAME interface got the downgrade edge
        # appended on top of the existing one, rendering `implements B, B` - a real
        # javac "repeated interface" error. A single generic pass here (rather than
        # guarding every append call site individually) also catches any future rule
        # that appends an edge without knowing this class of duplicate risk exists.
        seen_final_edges: Set[Tuple[str, str, str]] = set()
        deduped_edges = []
        for r in structural_edges:
            key = (r.from_entity, r.to_entity, r.type)
            if key in seen_final_edges:
                self.log_action("2.13_duplicate_edge_cleanup", r.from_entity, f"duplicate {r.type} edge to {r.to_entity} created during repair", "dropped duplicate")
                continue
            seen_final_edges.add(key)
            deduped_edges.append(r)
        structural_edges = deduped_edges

        # 2.14 Final structural-edge validity sweep. 2.10's checks (interface-state,
        # implements-target-must-be-an-interface) run BEFORE 2.10c, and 2.10c can
        # itself append a NEW `implements` edge as part of its own downgrade logic -
        # so an edge created there skips 2.10's filters entirely. Found live via
        # fuzzing TWICE the same way: first `interface E5 implements E1` (source is an
        # interface), then a class `implements` a target that ISN'T an interface
        # (2.10's OTHER check, same bypass). This must be the LAST structural check in
        # the function, after every rule that can append an edge has already run,
        # rather than another mid-pipeline filter a later rule could bypass again.
        final_swept_edges = []
        for r in structural_edges:
            source_is_interface = r.from_entity in valid_entity_names and entities[r.from_entity.lower()].is_interface
            if source_is_interface:
                self.log_action("2.14_interface_outgoing_edge_sweep", r.from_entity, f"interface cannot hold state or implement ({r.type} to {r.to_entity})", "dropped edge")
                continue
            if r.type == "implements" and r.to_entity in valid_entity_names and not entities[r.to_entity.lower()].is_interface:
                self.log_action("2.14_implements_target_sweep", r.from_entity, f"target {r.to_entity} is not an interface", "downgraded implements to association")
                r.type = "association"
            final_swept_edges.append(r)
        structural_edges = final_swept_edges

        # 2.15 Has-a field-name collision cleanup. JavaBuilder names a has-a field
        # after its TARGET only (`{target.lower()}s`), not the relationship type - so
        # if the SAME (from, to) pair survives as both e.g. a `composition` AND an
        # `aggregation` edge (two different, redundant declarations of a relationship
        # between the same two entities), JavaBuilder emits two fields with the
        # identical name, a real javac "variable already defined" error. Found live via
        # fuzzing. At most one has-a-family edge is kept per (from, to) pair,
        # regardless of which specific has-a type it is - `implements` is a different
        # field-less concept and is NOT constrained by this (an entity implementing an
        # interface AND separately having a has-a edge to some other, unrelated entity
        # of the same name pair can't happen since names are unique per entity anyway).
        HAS_A_TYPES = {"composition", "aggregation", "association"}
        seen_has_a_pairs: Set[Tuple[str, str]] = set()
        collision_free_edges = []
        for r in structural_edges:
            if r.type in HAS_A_TYPES:
                pair = (r.from_entity, r.to_entity)
                if pair in seen_has_a_pairs:
                    self.log_action("2.15_has_a_collision_cleanup", r.from_entity, f"another has-a edge to {r.to_entity} already claims this field name", "dropped edge")
                    continue
                seen_has_a_pairs.add(pair)
            collision_free_edges.append(r)
        structural_edges = collision_free_edges

        # 2.16 Inherited has-a field-name collision cleanup. Same root cause as 2.15,
        # but across an inheritance boundary: JavaBuilder.render_class merges a
        # subclass's OWN fields with every ancestor's fields into one constructor
        # (get_inherited_fields), so a subclass independently declaring a has-a edge to
        # the same target one of its ancestors already has produces the identical
        # duplicate-field/duplicate-constructor-parameter javac error 2.15 fixes for a
        # single entity - just one level removed. Found live via fuzzing (`E1 extends
        # E5`, both with a has-a edge to `E0` -> `public E1(List<E0> e0s, List<E0>
        # e0s)`). Favor the ancestor's field: it's already inherited and accessible, so
        # dropping the subclass's redundant redeclaration loses no real capability.
        has_a_targets_by_entity: Dict[str, Set[str]] = defaultdict(set)
        for r in structural_edges:
            if r.type in HAS_A_TYPES:
                has_a_targets_by_entity[r.from_entity].add(r.to_entity)

        def inherited_has_a_targets(name: str) -> Set[str]:
            targets: Set[str] = set()
            ancestor = class_parent_map.get(name)
            while ancestor:
                targets |= has_a_targets_by_entity.get(ancestor, set())
                ancestor = class_parent_map.get(ancestor)
            return targets

        inheritance_collision_free_edges = []
        for r in structural_edges:
            if r.type in HAS_A_TYPES and r.to_entity in inherited_has_a_targets(r.from_entity):
                self.log_action("2.16_inherited_has_a_collision_cleanup", r.from_entity, f"an ancestor already has a has-a edge to {r.to_entity}", "dropped edge")
                continue
            inheritance_collision_free_edges.append(r)
        structural_edges = inheritance_collision_free_edges

        return SketchPlan(
            design_rationale=sketch.design_rationale,
            entities=list(entities.values()),
            relationships=structural_edges
        )
