from src.schemas.logical_plan import LogicalPlan, SemanticEntity

class MermaidBuilder:
    def __init__(self):
        pass

    def build_class_diagram(self, plan: LogicalPlan) -> str:
        lines = ["classDiagram"]
        
        all_entities = plan.domain_entities + plan.support_entities
        
        for e in all_entities:
            # Add stereotypes
            if e.is_interface:
                lines.append(f"    class {e.name} {{\n        <<interface>>\n    }}")
            elif e.is_abstract:
                lines.append(f"    class {e.name} {{\n        <<abstract>>\n    }}")
            else:
                lines.append(f"    class {e.name}")
                
            # Inherits
            if e.inherits_from:
                lines.append(f"    {e.inherits_from} <|-- {e.name}")
                
            # Implements
            if e.implements:
                for imp in e.implements:
                    lines.append(f"    {imp} <|.. {e.name}")
                    
            # Composes
            if e.composes_with:
                for comp in e.composes_with:
                    lines.append(f"    {e.name} *-- {comp}")

            # Aggregates (hollow diamond - weaker ownership than composition)
            if e.aggregates_with:
                for agg in e.aggregates_with:
                    lines.append(f"    {e.name} o-- {agg}")

            # Associates
            if e.associated_with:
                for assoc in e.associated_with:
                    lines.append(f"    {e.name} --> {assoc}")
                    
        return "\n".join(lines)

    def build_detailed_diagram(self, ast_classes: list) -> str:
        lines = ["classDiagram"]
        
        # We need a way to map access modifiers
        def map_access(mod: str) -> str:
            if "public" in mod: return "+"
            if "private" in mod: return "-"
            if "protected" in mod: return "#"
            return "~"
            
        for cls in ast_classes:
            lines.append(f"    class {cls.name} {{")
            if cls.is_interface:
                lines.append("        <<interface>>")
            elif cls.is_abstract:
                lines.append("        <<abstract>>")
                
            for field in cls.fields:
                access = map_access(field.modifier)
                type_str = field.type_ref.name
                if field.type_ref.is_collection:
                    type_str = f"List~{type_str}~"
                lines.append(f"        {access}{type_str} {field.name}")
                
            for method in cls.methods:
                access = map_access(method.modifier)
                ret_str = method.return_type.name if method.return_type else "void"
                if method.return_type and method.return_type.is_collection:
                    ret_str = f"List~{ret_str}~"
                params = ", ".join([f"{p.type_ref.name} {p.name}" for p in method.parameters])
                abstract_mark = "*" if method.is_abstract else ""
                lines.append(f"        {access}{method.name}({params}){abstract_mark} {ret_str}")
                
            lines.append("    }")
            
            # Relationships
            if cls.extends:
                lines.append(f"    {cls.extends} <|-- {cls.name}")
            for imp in cls.implements:
                lines.append(f"    {imp} <|.. {cls.name}")
                
            # For composition/association, we infer from fields
            # Only draw arrows if the target type is a class within our domain (ast_classes)
            domain_class_names = {c.name for c in ast_classes}
            for field in cls.fields:
                target_type = field.type_ref.name
                if target_type in domain_class_names:
                    if field.type_ref.is_collection:
                        lines.append(f"    {cls.name} *-- {target_type}")
                    else:
                        lines.append(f"    {cls.name} --> {target_type}")
                        
        return "\n".join(lines)
