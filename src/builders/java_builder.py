import os
from src.schemas.logical_plan import LogicalPlan, SemanticEntity
from src.schemas.java_ast import JavaClass, JavaField, JavaTypeRef

class JavaBuilder:
    def __init__(self):
        # Instance attribute (not a local var) so the compile-verification gate (Phase 6)
        # can register newly-discovered stdlib type imports and have them stick across renders.
        self.standard_types = {
            "Map": "java.util.Map",
            "Set": "java.util.Set",
            "Date": "java.util.Date",
            "LocalDate": "java.time.LocalDate",
            "LocalDateTime": "java.time.LocalDateTime",
            "Scanner": "java.util.Scanner",
            "File": "java.io.File",
            "IOException": "java.io.IOException",
            "BigDecimal": "java.math.BigDecimal",
            "BigInteger": "java.math.BigInteger",
        }

    def build_ast(self, plan: LogicalPlan) -> list[JavaClass]:
        ast_nodes = []
        all_entities = plan.domain_entities + plan.support_entities
        
        for e in all_entities:
            fields = []
            
            # Composition -> List<Target>
            if e.composes_with:
                for comp in e.composes_with:
                    fields.append(
                        JavaField(
                            modifier="private",
                            type_ref=JavaTypeRef(name=comp, is_collection=True),
                            name=f"{comp.lower()}s"
                        )
                    )
            
            # Aggregation -> List<Target> (same Java shape as composition; the only
            # difference is ownership strength, which only the diagram distinguishes)
            if e.aggregates_with:
                for agg in e.aggregates_with:
                    fields.append(
                        JavaField(
                            modifier="private",
                            type_ref=JavaTypeRef(name=agg, is_collection=True),
                            name=f"{agg.lower()}s"
                        )
                    )

            # Association -> Target
            if e.associated_with:
                for assoc in e.associated_with:
                    fields.append(
                        JavaField(
                            modifier="private",
                            type_ref=JavaTypeRef(name=assoc, is_collection=False),
                            name=f"{assoc.lower()}"
                        )
                    )
            
            ast_nodes.append(
                JavaClass(
                    name=e.name,
                    is_abstract=e.is_abstract,
                    is_interface=e.is_interface,
                    extends=e.inherits_from,
                    implements=e.implements if e.implements else [],
                    fields=fields,
                    methods=[]
                )
            )
            
        return ast_nodes
        
    def get_inherited_fields(self, class_name: str, all_classes_map: dict[str, JavaClass]) -> list[JavaField]:
        fields = []
        if not all_classes_map or class_name not in all_classes_map:
            return fields
            
        cls = all_classes_map[class_name]
        if cls.extends and cls.extends in all_classes_map:
            parent_name = cls.extends
            fields.extend(self.get_inherited_fields(parent_name, all_classes_map))
            fields.extend(all_classes_map[parent_name].fields)
            
        return fields

    def render_class(self, java_class: JavaClass, all_classes_map: dict[str, JavaClass] = None) -> str:
        lines = []
        imports = set()
        
        # Check for collections - includes inherited fields, since they flow into this
        # class's auto-generated constructor signature (super(...) call) even though they
        # aren't declared on this class directly.
        inherited_fields = self.get_inherited_fields(java_class.name, all_classes_map) if all_classes_map else []
        needs_list = any(f.type_ref.is_collection for f in list(java_class.fields) + inherited_fields)
        for m in java_class.methods:
            if m.return_type and m.return_type.is_collection: needs_list = True
            for p in m.parameters:
                if p.type_ref.is_collection: needs_list = True
        
        if needs_list:
            imports.add("java.util.List")
            imports.add("java.util.ArrayList")
            
        # Detect standard java types
        def check_type_import(type_name: str):
            if type_name in self.standard_types:
                imports.add(self.standard_types[type_name])
                
        for f in java_class.fields: check_type_import(f.type_ref.name)
        for m in java_class.methods:
            if m.return_type: check_type_import(m.return_type.name)
            for p in m.parameters: check_type_import(p.type_ref.name)
            
        # Write Imports
        for imp in sorted(imports):
            lines.append(f"import {imp};")
        if imports:
            lines.append("")
            
        # Class declaration
        modifiers = ["public"]
        if java_class.is_interface:
            modifiers.append("interface")
        else:
            if java_class.is_abstract:
                modifiers.append("abstract")
            modifiers.append("class")
            
        decl = " ".join(modifiers) + f" {java_class.name}"
        if java_class.extends: decl += f" extends {java_class.extends}"
        if java_class.implements: decl += f" implements {', '.join(java_class.implements)}"
            
        lines.append(f"{decl} {{")
        
        # Fields
        if not java_class.is_interface:
            for f in java_class.fields:
                type_str = f"List<{f.type_ref.name}>" if f.type_ref.is_collection else f.type_ref.name
                lines.append(f"    {f.modifier} {type_str} {f.name};")
            if java_class.fields: lines.append("")
                
            # Constructor
            inherited_fields = self.get_inherited_fields(java_class.name, all_classes_map) if all_classes_map else []
            all_constructor_fields = inherited_fields + java_class.fields
            
            if all_constructor_fields:
                param_strs = []
                init_lines = []
                super_param_names = []
                
                # Inherited fields
                for f in inherited_fields:
                    type_str = f"List<{f.type_ref.name}>" if f.type_ref.is_collection else f.type_ref.name
                    param_strs.append(f"{type_str} {f.name}")
                    super_param_names.append(f.name)
                    
                # Own fields
                for f in java_class.fields:
                    type_str = f"List<{f.type_ref.name}>" if f.type_ref.is_collection else f.type_ref.name
                    param_strs.append(f"{type_str} {f.name}")
                    init_lines.append(f"        this.{f.name} = {f.name};")
                
                ctor_modifier = "protected" if java_class.is_abstract else "public"
                lines.append(f"    {ctor_modifier} {java_class.name}({', '.join(param_strs)}) {{")
                if super_param_names:
                    lines.append(f"        super({', '.join(super_param_names)});")
                lines.extend(init_lines)
                lines.append("    }")
                lines.append("")
                
            # Getters & Setters
            for f in java_class.fields:
                if f.modifier in ["private", "protected"]:
                    type_str = f"List<{f.type_ref.name}>" if f.type_ref.is_collection else f.type_ref.name
                    capitalized = f.name[0].upper() + f.name[1:]
                    
                    # Getter
                    lines.append(f"    public {type_str} get{capitalized}() {{")
                    lines.append(f"        return this.{f.name};")
                    lines.append("    }")
                    
                    # Setter
                    lines.append(f"    public void set{capitalized}({type_str} {f.name}) {{")
                    lines.append(f"        this.{f.name} = {f.name};")
                    lines.append("    }")
            if java_class.fields: lines.append("")
                
        # Methods
        for m in java_class.methods:
            ret = m.return_type.name if m.return_type else "void"
            if m.return_type and m.return_type.is_collection:
                ret = f"List<{ret}>"
                
            params = []
            for p in m.parameters:
                ptype = f"List<{p.type_ref.name}>" if p.type_ref.is_collection else p.type_ref.name
                params.append(f"{ptype} {p.name}")
                
            p_str = ", ".join(params)
            
            if java_class.is_interface or m.is_abstract:
                # An abstract method on a regular (non-interface) class MUST have the
                # literal `abstract` keyword written, or javac rejects it as "missing
                # method body, or declare abstract" - unlike an interface method, which
                # is implicitly abstract with no keyword needed. This was previously
                # unreachable: is_abstract was never actually set True on any method
                # before src/detail_pipeline.py's Phase 5a-i/5a-ii split started
                # threading it through from LLM signature generation.
                abstract_kw = "" if java_class.is_interface else "abstract "
                lines.append(f"    {m.modifier} {abstract_kw}{ret} {m.name}({p_str});")
            else:
                lines.append(f"    {m.modifier} {ret} {m.name}({p_str}) {{")
                if m.body:
                    for bline in m.body.split("\n"):
                        # Only indent non-empty lines properly
                        if bline.strip():
                            lines.append(f"        {bline.strip()}")
                lines.append("    }")
                
        lines.append("}")
        return "\n".join(lines)

    def build_and_save(self, plan: LogicalPlan, output_dir: str):
        ast_nodes = self.build_ast(plan)
        
        import shutil
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir, exist_ok=True)
        
        # Dump AST to JSON for manual inspection - Phase 4's output (AST Bootstrap),
        # consumed by src/detail_pipeline.py to start Phase 5a-i.
        import json
        with open(os.path.join("output", "phase4_ast_bootstrap.json"), "w", encoding="utf-8") as f:
            json.dump([node.model_dump() for node in ast_nodes], f, indent=2)
            
        all_classes_map = {node.name: node for node in ast_nodes}
            
        for node in ast_nodes:
            code = self.render_class(node, all_classes_map)
            file_path = os.path.join(output_dir, f"{node.name}.java")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
