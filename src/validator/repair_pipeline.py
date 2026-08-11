import logging
from collections import defaultdict
from typing import List, Dict, Set, Tuple
from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.schemas.logical_plan import SketchPlan, LogicalPlan, SemanticEntity

logger = logging.getLogger(__name__)

class StructuralRepairPipeline:
    def __init__(self, preset: BlueprintPreset, domain: DomainConfig):
        self.preset = preset
        self.domain = domain
        self.action_log = []

    def log_action(self, step: str, node: str, detail: str, action: str):
        self.action_log.append({
            "step": step,
            "node": node,
            "detail": detail,
            "action": action
        })
        logger.info(f"[{step}] Node '{node}': {detail} -> {action}")

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
        
        # Split edges, remove self-loops, and prune dangling references
        inheritance_edges = []
        structural_edges = []
        
        for r in sketch.relationships:
            r.from_entity = name_to_canonical.get(r.from_entity, r.from_entity)
            r.to_entity = name_to_canonical.get(r.to_entity, r.to_entity)
            
            if r.from_entity not in valid_entity_names or r.to_entity not in valid_entity_names:
                self.log_action("2.0_dangling", r.from_entity, f"edge points to missing entity ({r.from_entity} -> {r.to_entity})", "dropped edge")
                continue
                
            if r.from_entity == r.to_entity:
                self.log_action("2.0_normalization", r.from_entity, "self-loop detected", "dropped edge")
                continue
                
            if r.type == "inheritance":
                inheritance_edges.append(r)
            else:
                structural_edges.append(r)

        # 2.9 Convergence Guard
        converged = False
        iteration = 0
        while not converged and iteration < 10:
            iteration += 1
            converged = True
            
            # 2.1 Inheritance Enforcement (Forest)
            parent_map = {}
            valid_inheritance = []
            for r in inheritance_edges:
                child = r.from_entity
                parent = r.to_entity
                if child in parent_map:
                    self.log_action("2.1_multiple_inheritance", child, f"already inherits from {parent_map[child]}, ignoring {parent}", "downgraded to implements")
                    r.type = "implements"
                    structural_edges.append(r)
                    converged = False
                else:
                    parent_map[child] = parent
                    valid_inheritance.append(r)
                    
            # Break cycles in the parent_map
            WHITE, GRAY, BLACK = 0, 1, 2
            color = {n: WHITE for n in valid_entity_names}
            broken_edges = set()
            
            def dfs_inherit(node):
                color[node] = GRAY
                parent = parent_map.get(node)
                if parent:
                    if color.get(parent, WHITE) == GRAY:
                        self.log_action("2.1_cycle_detection", node, f"inheritance cycle detected involving {parent}", "broke edge, downgraded to association")
                        broken_edges.add((node, parent))
                    elif color.get(parent, WHITE) == WHITE:
                        dfs_inherit(parent)
                color[node] = BLACK
                
            for n in list(parent_map):
                if color.get(n, WHITE) == WHITE:
                    dfs_inherit(n)
                    
            # Re-filter valid inheritance
            final_inheritance = []
            for r in valid_inheritance:
                if (r.from_entity, r.to_entity) in broken_edges:
                    r.type = "association"
                    structural_edges.append(r)
                    del parent_map[r.from_entity]
                    converged = False
                else:
                    final_inheritance.append(r)
                    
            inheritance_edges = final_inheritance
    
            # 2.3 Depth Constraint
            max_depth = self.preset.oop.inheritance.max_depth if self.preset.oop and self.preset.oop.inheritance else None
            if max_depth is not None:
                depth_map = {}
                def get_depth(node):
                    if node not in depth_map:
                        p = parent_map.get(node)
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
                            temp = parent_map.get(temp)
                        path.reverse()
                        
                        if len(path) > max_depth:
                            new_parent = path[max_depth - 1]
                            self.log_action("2.3_depth_exceeded", n, f"depth={d} > max={max_depth}", f"reattached to {new_parent}")
                            parent_map[n] = new_parent
                            depth_map.clear()
                            converged = False
                
                new_inheritance_edges = []
                for child, parent in parent_map.items():
                    existing = next((r for r in inheritance_edges if r.from_entity == child and r.to_entity == parent), None)
                    if existing:
                        new_inheritance_edges.append(existing)
                    else:
                        from src.schemas.logical_plan import SketchRelationship
                        new_inheritance_edges.append(SketchRelationship(from_entity=child, to_entity=parent, type="inheritance"))
                inheritance_edges = new_inheritance_edges
    
            # Helper for transitive ancestor checking
            def ancestors(node: str) -> set:
                ancs = set()
                curr = parent_map.get(node)
                while curr:
                    ancs.add(curr)
                    curr = parent_map.get(curr)
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
                    deg += sum(1 for r in inheritance_edges if r.from_entity == node or r.to_entity == node)
                    if deg == 0: s += 50
                    elif deg == 1: s += 5
                    is_parent = any(p == node for p in parent_map.values())
                    if not is_parent: s += 20
                    return s
                
                sorted_nodes = sorted(valid_entity_names, key=lambda n: score(n), reverse=True)
                node_to_drop = sorted_nodes[0]
                self.log_action("2.5_excess_classes", node_to_drop, f"total classes {len(valid_entity_names)} > {max_classes}", "dropped entity")
                
                valid_entity_names.remove(node_to_drop)
                del entities[node_to_drop.lower()]
                
                inheritance_edges = [r for r in inheritance_edges if r.from_entity != node_to_drop and r.to_entity != node_to_drop]
                structural_edges = [r for r in structural_edges if r.from_entity != node_to_drop and r.to_entity != node_to_drop]
                
                converged = False
                
            # 2.8 Disconnected component check
            adj = defaultdict(set)
            for r in inheritance_edges + structural_edges:
                adj[r.from_entity].add(r.to_entity)
                adj[r.to_entity].add(r.from_entity)
                
            for node in list(valid_entity_names):
                if not adj[node]:
                    if entities[node.lower()].kind == "core":
                        other_cores = [n for n in valid_entity_names if n != node and entities[n.lower()].kind == "core"]
                        target = other_cores[0] if other_cores else (list(valid_entity_names - {node})[0] if valid_entity_names - {node} else None)
                        if target:
                            self.log_action("2.8_isolated_core", node, "core node is isolated", f"added association to {target}")
                            from src.schemas.logical_plan import SketchRelationship
                            structural_edges.append(SketchRelationship(from_entity=node, to_entity=target, type="association"))
                            adj[node].add(target)
                            adj[target].add(node)
                            converged = False
                    else:
                        self.log_action("2.8_isolated_non_core", node, "non-core node is isolated", "dropped entity")
                        valid_entity_names.remove(node)
                        del entities[node.lower()]
                        converged = False
                        
        if not converged:
            logger.warning(f"Repair pipeline did not converge after {iteration} iterations")
            print(f"[WARNING] Repair pipeline did not converge after {iteration} iterations")
            
        # 2.6 Interfaces & 2.6b Abstractions
        parent_map = {r.from_entity: r.to_entity for r in inheritance_edges}
        children_count = defaultdict(int)
        for p in parent_map.values():
            children_count[p] += 1
            
        implements_target_count = defaultdict(int)
        for r in structural_edges:
            if r.type == "implements":
                implements_target_count[r.to_entity] += 1
                
        interface_enabled = self.preset.oop.interface.enabled if self.preset.oop and self.preset.oop.interface else False
        abstraction_enabled = self.preset.oop.abstraction.enabled if self.preset.oop and self.preset.oop.abstraction else False
        
        for node in valid_entity_names:
            e = entities[node.lower()]
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
                elif abstraction_enabled and not e.is_interface:
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
                if e.is_interface and r.type in ["composition", "aggregation", "association"]:
                    self.log_action("2.10_interface_state", r.from_entity, f"interface cannot hold state ({r.type} to {r.to_entity})", "dropped edge")
                    continue
                    
            final_structural_edges.append(r)
        structural_edges = final_structural_edges

        # 2.10b Interface cannot extend a class (only another interface)
        final_inheritance_edges = []
        for r in inheritance_edges:
            child_is_interface = entities[r.from_entity.lower()].is_interface
            parent_is_interface = entities[r.to_entity.lower()].is_interface
            if child_is_interface and not parent_is_interface:
                self.log_action("2.10_interface_extends_class", r.from_entity, f"interface cannot extend class {r.to_entity}", "dropped inheritance edge")
                continue
            final_inheritance_edges.append(r)
        inheritance_edges = final_inheritance_edges

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
        for r in inheritance_edges:
            final_children_count[r.to_entity] += 1

        for node in valid_entity_names:
            e = entities[node.lower()]
            if e.is_interface:
                has_implementer = final_implements_count.get(node, 0) >= 1 or final_children_count.get(node, 0) >= 1
                if not has_implementer:
                    self.log_action("2.11_orphan_interface", node, "interface has zero implementers/inheritors after repair", "demoted to concrete class")
                    e.is_interface = False

        return SketchPlan(
            design_rationale=sketch.design_rationale,
            entities=list(entities.values()),
            relationships=inheritance_edges + structural_edges
        )
