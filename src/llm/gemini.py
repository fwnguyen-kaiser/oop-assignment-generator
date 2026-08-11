import os
import tenacity
from google import genai
from google.genai import types
from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.schemas.logical_plan import SketchPlan

class GeminiProvider:
    def __init__(self, api_key: str):
        # The new google-genai SDK uses client initialization
        self.client = genai.Client(api_key=api_key)
        self.model_name = "gemini-3.5-flash-lite"

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=5, max=65), stop=tenacity.stop_after_attempt(5))
    def generate_sketch(
        self, 
        preset: BlueprintPreset, 
        sanitized_domain: DomainConfig, 
        rag_context: str,
        previous_errors: list[str] = None
    ) -> SketchPlan:
        
        system_instruction = (
            "You are an expert Software Architect tasked with brainstorming a Domain-First Object-Oriented system.\n"
            "You are in Phase 1 (Sketch Phase). Your job is to generate a semantic sketch of entities and relationships."
        )
        
        few_shot_example = """
--- FEW-SHOT EXAMPLE ---
If the domain is 'E-Commerce' and soft guidance asks for ~4 classes:
{
  "design_rationale": "Modeled a basic e-commerce flow with Order composing LineItem.",
  "entities": [
    {"name": "Order", "kind": "core", "note": "Customer's purchase order"},
    {"name": "LineItem", "kind": "core", "note": "Individual item in the order"},
    {"name": "Product", "kind": "core", "note": "Catalog item"},
    {"name": "PaymentProcessor", "kind": "supporting", "note": "External service"}
  ],
  "relationships": [
    {"from_entity": "Order", "to_entity": "LineItem", "type": "composition"},
    {"from_entity": "LineItem", "to_entity": "Product", "type": "association"}
  ]
}
"""

        prompt = [
            "\n--- DOMAIN CONTEXT ---",
            f"Topic: {sanitized_domain.name}",
            f"Description: {sanitized_domain.description}",
            f"Keywords: {', '.join(sanitized_domain.keywords)}",
            "WARNING: The 'entity_hints' and 'relationship_hints' provided below are merely VOCABULARY INSPIRATIONS.",
            "CREATIVITY REQUIRED: You MUST NOT blindly copy all the hints. You are EXPECTED to invent NOVEL entities that are NOT explicitly listed in the hints, as long as they fit the Domain Description.",
            f"Hints: {sanitized_domain.entity_hints.model_dump_json(exclude_none=True) if sanitized_domain.entity_hints else 'None'}",
            f"Relationship Hints: {sanitized_domain.relationship_hints.model_dump_json(exclude_none=True) if sanitized_domain.relationship_hints else 'None'}",
            
            "\n--- SOFT STRUCTURAL GUIDANCE ---",
            "This is just soft guidance. Do your best to generate roughly this many entities, but do NOT worry about being perfectly precise.",
            f"- Aim for around {preset.structure.classes.min} to {preset.structure.classes.max} domain classes.",
            f"- Aim for a maximum inheritance depth of roughly {preset.oop.inheritance.max_depth if preset.oop and preset.oop.inheritance else 'N/A'}.",
            "- HARD RULE (not soft): if you set `is_interface: true` on ANY entity, you MUST also include at "
            "least one relationship of type 'implements' targeting that entity in this SAME response. An "
            "interface with no implementer is useless to the student - never create one without its implementer.",

            few_shot_example,
            
            "\n--- RAG CONTEXT ---\n" + rag_context
        ]

        if previous_errors:
            prompt.append("\n--- CRITICAL ERROR FEEDBACK ---")
            prompt.append("Your previous sketch failed validation with these errors:")
            for e in previous_errors:
                prompt.append(f"- {e}")
            prompt.append("Please fix these errors in your new sketch. Dangling references must be fixed by ensuring 'from_entity' and 'to_entity' exactly match names in the 'entities' list.")

        response = self.client.models.generate_content(
            model=self.model_name,
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=SketchPlan,
                temperature=0.7 # Add a bit of creativity for brainstorming
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=5, max=65), stop=tenacity.stop_after_attempt(5))
    def generate_missing_entities(
        self,
        current_sketch: SketchPlan,
        domain: DomainConfig,
        missing_count: int
    ) -> SketchPlan:
        system_instruction = (
            "You are an expert Software Architect.\n"
            "You are in Phase 1b (Targeted Addition). The current design does not have enough entities to meet the minimum requirement."
        )
        
        prompt = [
            f"--- DOMAIN CONTEXT ---\nTopic: {domain.name}\nDescription: {domain.description}",
            f"\n--- CURRENT DESIGN ---\n{current_sketch.model_dump_json(indent=2)}",
            f"\n--- TASK ---\nWe need exactly {missing_count} NEW entities to be added to the design.",
            "Please generate a JSON containing ONLY the new entities and the relationships connecting them to the existing design.",
            "Do NOT output the existing entities again. Only output the newly invented ones.",
            "HARD RULE: if you set `is_interface: true` on any new entity, you MUST also include a relationship "
            "of type 'implements' targeting it in this same response."
        ]

        response = self.client.models.generate_content(
            model=self.model_name,
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=SketchPlan,
                temperature=0.7
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=5, max=65), stop=tenacity.stop_after_attempt(5))
    def generate_missing_interface(
        self,
        current_sketch: SketchPlan,
        domain: DomainConfig
    ) -> SketchPlan:
        system_instruction = (
            "You are an expert Software Architect.\n"
            "You are in Phase 1c (Targeted Interface Addition). The current design has no interface, but the "
            "assignment's difficulty preset requires students to practice interface usage.\n"
            "Do NOT force any EXISTING entity to become an interface - that destroys its state/relationships and "
            "produces a nonsensical design. Instead, propose exactly ONE new interface entity that makes real "
            "domain sense (e.g. a shared capability/behavior contract), and specify which existing entity (or a "
            "new small one, if genuinely needed) should implement it via a relationship of type 'implements'."
        )

        prompt = [
            f"--- DOMAIN CONTEXT ---\nTopic: {domain.name}\nDescription: {domain.description}",
            f"\n--- CURRENT DESIGN ---\n{current_sketch.model_dump_json(indent=2)}",
            "\n--- TASK ---\nPropose exactly ONE new interface entity (set is_interface=true) plus the "
            "'implements' relationship connecting an entity to it.",
            "Output ONLY the new entity/entities and the new relationship(s). Do NOT repeat existing entities.",
            "If this domain genuinely has no sensible interface concept, still propose the most plausible one "
            "rather than leaving it empty - a soft/optional capability is acceptable."
        ]

        response = self.client.models.generate_content(
            model=self.model_name,
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=SketchPlan,
                temperature=0.7
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=5, max=65), stop=tenacity.stop_after_attempt(5))
    def generate_design_decisions(
        self,
        final_sketch: SketchPlan,
        action_log: list[dict]
    ) -> list[str]:
        system_instruction = (
            "You are an expert Software Architect.\n"
            "You are in Phase 3 (Documentation). Your job is to generate a list of design decisions based on the final structural graph and the pipeline's action logs."
        )
        
        prompt = [
            f"--- FINAL SKETCH ---\n{final_sketch.model_dump_json(indent=2)}",
            f"\n--- ACTION LOG (STRUCTURAL COMPROMISES) ---\n" + "\n".join([f"{log['step']}: {log['detail']} -> {log['action']}" for log in action_log]),
            "\n--- TASK ---",
            "Generate a JSON array of strings explaining the final design decisions.",
            "Explain the domain rationale, and importantly, acknowledge any structural compromises the pipeline made (e.g. 'Downgraded multiple inheritance to association because Java does not support it')."
        ]
        
        from pydantic import BaseModel
        class DecisionsResponse(BaseModel):
            decisions: list[str]

        response = self.client.models.generate_content(
            model=self.model_name,
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=DecisionsResponse,
                temperature=0.3
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed.decisions

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=5, max=65), stop=tenacity.stop_after_attempt(5))
    def enrich_ast_with_details(self, java_ast_json: str, domain_name: str, domain_desc: str) -> list:
        system_instruction = (
            "You are an expert Java Developer.\n"
            "You are in Phase 4 (Detailed Code Generation). Your job is to fill in the missing attributes and methods for the provided AST."
        )
        
        prompt = [
            f"--- DOMAIN CONTEXT ---\nTopic: {domain_name}\nDescription: {domain_desc}",
            "\n--- CURRENT AST (STRUCTURAL) ---",
            "The following JSON represents the structural skeleton of the system. Note that relationship fields (e.g. List<Target>) are ALREADY present.",
            java_ast_json,
            "\n--- TASK ---",
            "For each class in the AST, invent basic primitive fields (like String name, double balance, int hp) and business methods (like attack(), deposit()).",
            "Provide a VERY SHORT stub body for each method (e.g. 'return 0;', 'this.hp -= damage;').",
            "Always prefix field access with 'this.' (e.g. 'this.balance', not bare 'balance') for clarity, including fields inherited from a parent class.",
            "Do NOT invent getter/setter methods (e.g. getBalance(), setBalance()) for any field - those are generated automatically. Only invent real business-logic methods.",
            "If a class has a non-empty `implements` list, decide that interface's method signature(s) FIRST, then make sure the implementing class includes a method with the EXACT same name and parameter list (this applies even if the interface class itself appears later in this same AST/response).",
            "Do NOT output the existing relationship fields again. Only output the NEW fields and methods."
        ]
        
        from src.schemas.java_ast import DetailsResponse
        
        response = self.client.models.generate_content(
            model=self.model_name, # Use existing flash model
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=DetailsResponse,
                temperature=0.4
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed.entities

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=3, max=15), stop=tenacity.stop_after_attempt(2))
    def fix_compile_errors(self, broken_classes: list, javac_stderr: str, domain_name: str, domain_desc: str) -> list:
        """Phase 6, Tier 2 - last-resort repair. Only called after the deterministic Tier 1
        pattern-fixes couldn't resolve a real javac failure. Kept to a small retry budget
        (caller also caps total attempts) since this is a costly, best-effort safeguard, not
        the primary correctness mechanism."""
        system_instruction = (
            "You are an expert Java Developer performing emergency compile-error repair.\n"
            "You are given classes that FAILED TO COMPILE with the real javac compiler, plus the exact compiler "
            "error output. Fix ONLY what the errors point to - do not redesign the class. Return the corrected "
            "field and method list for each affected class (structural relationship fields/methods that were "
            "already correct should be preserved as given)."
        )

        classes_json = "\n".join(
            f"--- Class: {c.name} ---\n" + c.model_dump_json(indent=2) for c in broken_classes
        )

        prompt = [
            f"--- DOMAIN CONTEXT ---\nTopic: {domain_name}\nDescription: {domain_desc}",
            f"\n--- CLASSES THAT FAILED TO COMPILE ---\n{classes_json}",
            f"\n--- REAL javac ERROR OUTPUT ---\n{javac_stderr}",
            "\n--- TASK ---\nReturn the corrected fields and methods for each affected class listed above."
        ]

        from src.schemas.java_ast import DetailsResponse

        response = self.client.models.generate_content(
            model=self.model_name,
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=DetailsResponse,
                temperature=0.3
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed.entities

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=2, min=5, max=65), stop=tenacity.stop_after_attempt(5))
    def fill_missing_contract_methods(self, missing: dict, domain_name: str, domain_desc: str) -> list:
        system_instruction = (
            "You are an expert Java Developer.\n"
            "You are filling in REQUIRED method implementations that are missing from an interface or abstract-class "
            "contract. Each method MUST get a short, real, domain-appropriate body — not a placeholder."
        )

        lines = [
            f"--- DOMAIN CONTEXT ---\nTopic: {domain_name}\nDescription: {domain_desc}",
            "\n--- MISSING CONTRACT METHODS ---",
        ]
        for class_name, methods in missing.items():
            for m in methods:
                ret = m.return_type.name if m.return_type else "void"
                params = ", ".join(f"{p.type_ref.name} {p.name}" for p in m.parameters)
                lines.append(f"- Class '{class_name}' must implement: {ret} {m.name}({params})")

        lines.append("\n--- TASK ---")
        lines.append(
            "For each entry above, provide a short, real Java statement body (e.g. 'return this.balance * this.rate;'). "
            "Do NOT return empty or trivial placeholder bodies like 'return 0;' unless the domain genuinely has no better answer. "
            "Always prefix field access with 'this.' (e.g. 'this.balance', not bare 'balance'), including fields inherited from a parent class."
        )

        from src.schemas.java_ast import ContractFillResponse

        response = self.client.models.generate_content(
            model=self.model_name,
            contents="\n".join(lines),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=ContractFillResponse,
                temperature=0.4
            ),
        )

        if response.parsed is None:
            raise ValueError(f"Structured output parse failed. Raw text: {response.text}")

        return response.parsed.fills
