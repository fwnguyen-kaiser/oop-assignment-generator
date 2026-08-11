from pydantic import BaseModel, Field
from typing import List, Optional

class JavaTypeRef(BaseModel):
    name: str
    is_collection: bool = False

class JavaField(BaseModel):
    modifier: str = "private"
    type_ref: JavaTypeRef
    name: str

class JavaParameter(BaseModel):
    type_ref: JavaTypeRef
    name: str

class JavaMethod(BaseModel):
    modifier: str = "public"
    return_type: Optional[JavaTypeRef] = None
    name: str
    parameters: List[JavaParameter] = []
    body: Optional[str] = None
    is_abstract: bool = False

class JavaClass(BaseModel):
    name: str
    is_abstract: bool = False
    is_interface: bool = False
    extends: Optional[str] = None
    implements: List[str] = []
    fields: List[JavaField] = []
    methods: List[JavaMethod] = []

class DetailedEntity(BaseModel):
    name: str = Field(description="The name of the class exactly as provided")
    fields: List[JavaField] = Field(description="Primitive or standard library fields (e.g., String name, int hp). Do NOT invent relationship fields like List<Weapon>, they are already handled.")
    methods: List[JavaMethod] = Field(description="Business logic methods. Include very short implementation bodies for standard methods (e.g. return 0, System.out.println).")

class DetailsResponse(BaseModel):
    entities: List[DetailedEntity]

class ContractFill(BaseModel):
    class_name: str = Field(description="Name of the class that needs this method implemented")
    method_name: str = Field(description="Exact method name being implemented, matching the required signature")
    body: str = Field(description="Short Java statement(s) implementing real domain logic for this method, no braces")

class ContractFillResponse(BaseModel):
    fills: List[ContractFill]
