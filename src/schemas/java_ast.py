import re
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

# Unlike SketchEntity.name (already validated PascalCase at Phase 1), field/method/
# parameter names had no identifier-safety check at all before this - found via an
# independent audit: nothing stopped an LLM structured-output response from putting an
# arbitrary string here, which would both break the rendered .java file (a real javac
# syntax error, though Phase 6 would eventually catch that) and flow unescaped into
# mermaid_builder.py's diagram output with no compiler-equivalent oracle to catch THAT.
# Validating at the schema boundary (where Gemini's response is actually parsed) is
# cheaper and clearer than only ever discovering it downstream.
_JAVA_IDENTIFIER = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$]*$')


def _validate_java_identifier(v: str) -> str:
    if not _JAVA_IDENTIFIER.match(v):
        raise ValueError(f"'{v}' is not a valid Java identifier")
    return v


class JavaTypeRef(BaseModel):
    name: str
    is_collection: bool = False

class JavaField(BaseModel):
    modifier: str = "private"
    type_ref: JavaTypeRef
    name: str

    @field_validator('name')
    @classmethod
    def name_must_be_valid_identifier(cls, v):
        return _validate_java_identifier(v)

class JavaParameter(BaseModel):
    type_ref: JavaTypeRef
    name: str

    @field_validator('name')
    @classmethod
    def name_must_be_valid_identifier(cls, v):
        return _validate_java_identifier(v)

class JavaMethod(BaseModel):
    modifier: str = "public"
    return_type: Optional[JavaTypeRef] = None
    name: str
    parameters: List[JavaParameter] = []
    body: Optional[str] = None
    is_abstract: bool = False

    @field_validator('name')
    @classmethod
    def name_must_be_valid_identifier(cls, v):
        return _validate_java_identifier(v)

class JavaClass(BaseModel):
    name: str
    is_abstract: bool = False
    is_interface: bool = False
    extends: Optional[str] = None
    # Interface-extends-interface only (JLS allows multiple parents there, unlike
    # the single-parent `extends` above which is for class/abstract-class only).
    extends_interfaces: List[str] = []
    implements: List[str] = []
    fields: List[JavaField] = []
    methods: List[JavaMethod] = []

class DetailedEntity(BaseModel):
    name: str = Field(description="The name of the class exactly as provided")
    fields: List[JavaField] = Field(description="Primitive or standard library fields (e.g., String name, int hp). Do NOT invent relationship fields like List<Weapon>, they are already handled.")
    methods: List[JavaMethod] = Field(description="Business logic methods. Include very short implementation bodies for standard methods (e.g. return 0, System.out.println).")

class DetailsResponse(BaseModel):
    entities: List[DetailedEntity]

# --- Phase 5a-i: Signature-only Schemas ---
# Structurally excludes `body` (unlike JavaMethod/DetailedEntity above) so the LLM
# cannot spend tokens writing implementation logic before ContentRepairPipeline has had
# a chance to dedupe/rename/drop colliding signatures - see
# src/validator/content_repair_pipeline.py's module docstring for why that ordering
# matters (previously, repair ran AFTER bodies were written, so a dropped/renamed
# method's already-generated body was pure wasted generation).

class SignatureMethod(BaseModel):
    modifier: str = "public"
    return_type: Optional[JavaTypeRef] = None
    name: str
    parameters: List[JavaParameter] = []
    is_abstract: bool = False

    @field_validator('name')
    @classmethod
    def name_must_be_valid_identifier(cls, v):
        return _validate_java_identifier(v)

class SignatureEntity(BaseModel):
    name: str = Field(description="The name of the class exactly as provided")
    fields: List[JavaField] = Field(description="Primitive or standard library fields (e.g., String name, int hp). Do NOT invent relationship fields like List<Weapon>, they are already handled.")
    methods: List[SignatureMethod] = Field(description="Business logic method SIGNATURES only - name, return type, parameters. Do NOT write implementation bodies here, they are requested in a separate step.")

class SignaturesResponse(BaseModel):
    entities: List[SignatureEntity]

class ContractFill(BaseModel):
    class_name: str = Field(description="Name of the class that needs this method implemented")
    method_name: str = Field(description="Exact method name being implemented, matching the required signature")
    param_types: List[str] = Field(
        default_factory=list,
        description="The exact parameter TYPE names, in order, of the specific method being filled (e.g. "
        "[\"int\", \"String\"] for foo(int a, String b)). Required to disambiguate overloaded methods - "
        "(class_name, method_name) alone is NOT a valid Java method identity, two methods can share a name "
        "with different parameters. Empty list for a no-arg method."
    )
    body: str = Field(description="Short Java statement(s) implementing real domain logic for this method, no braces")

class ContractFillResponse(BaseModel):
    fills: List[ContractFill]
