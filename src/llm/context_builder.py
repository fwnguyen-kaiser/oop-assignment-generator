import yaml
import os
from typing import Dict, Any
from src.schemas.blueprint import OopConfig

class ContextBuilder:
    def __init__(self, kb_path: str = "knowledge_base/concepts.yaml"):
        self.kb_path = kb_path
        self.concepts = self._load_kb()
        
    def _load_kb(self) -> Dict[str, Any]:
        if not os.path.exists(self.kb_path):
            return {}
        with open(self.kb_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
            
    def build_context(self, oop_config: OopConfig) -> str:
        """
        Dynamically builds the RAG context string based ONLY on enabled OOP features.
        """
        context_lines = []
        context_lines.append("# Knowledge Base (OOP Constraints & Examples)\n")
        context_lines.append("You MUST adhere to the following definitions and rules for the requested concepts:\n")
        
        # Check Inheritance
        if oop_config.inheritance is not None and oop_config.inheritance.enabled:
            context_lines.append(self._format_concept("inheritance"))
            
        # Check Abstraction
        if oop_config.abstraction and oop_config.abstraction.enabled:
            context_lines.append(self._format_concept("abstraction"))
            
        # Check Interface
        if oop_config.interface and oop_config.interface.enabled:
            context_lines.append(self._format_concept("interface"))
            
        # Check Composition
        if oop_config.composition is not None and oop_config.composition.enabled:
            context_lines.append(self._format_concept("composition"))
            
        # Check Aggregation
        if oop_config.aggregation and oop_config.aggregation.enabled:
            context_lines.append(self._format_concept("aggregation"))
            
        if len(context_lines) == 2:
            return "" # No OOP features enabled, no context needed
            
        return "\n".join(context_lines)
        
    def sanitize_domain(self, domain: Any, oop_config: OopConfig) -> Any:
        """
        Prunes relationship hints from the domain config if the corresponding OOP feature is disabled in the blueprint.
        This enforces 'Illegal states are unrepresentable'.
        """
        if not domain.relationship_hints:
            return domain
            
        if oop_config.inheritance is None or not oop_config.inheritance.enabled:
            domain.relationship_hints.inheritance = None
            
        if oop_config.composition is None or not oop_config.composition.enabled:
            domain.relationship_hints.composition = None
            
        return domain
        
    def _format_concept(self, concept_name: str) -> str:
        concept_data = self.concepts.get(concept_name)
        if not concept_data:
            return f"## {concept_name.upper()}\n(No definitions found in knowledge base)\n"
            
        lines = [f"## {concept_name.upper()}"]
        
        if "definition" in concept_data:
            lines.append("**Definition:**")
            for d in concept_data["definition"]:
                lines.append(f" - {d}")
                
        if "rules" in concept_data:
            lines.append("**Rules:**")
            for r in concept_data["rules"]:
                lines.append(f" - {r}")
                
        if "examples" in concept_data:
            lines.append("**Examples:**")
            for e in concept_data["examples"]:
                lines.append(f" - {e}")
                
        lines.append("")
        return "\n".join(lines)
