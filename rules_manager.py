import json
import os # Added import for os
from typing import List, Literal, Optional, Union, Dict
from enum import Enum
from datetime import date
from pydantic import BaseModel, Field, model_validator
import decimal
import uuid

# Helper function from actualpy documentation (assuming it exists or needs to be implemented)
def get_attribute_by_table_name(table_name, field):
    # This is a placeholder. In a real scenario, this would map field names to actual database column names.
    # For now, we'll assume a direct mapping or handle specific cases.
    if table_name == "transactions":
        if field == "description":
            return "payee_id"
        return field
    return field

def get_value(value, value_type):
    if value_type == ValueType.DATE and isinstance(value, str):
        return date.fromisoformat(value)
    return value

def is_uuid(value):
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False

class ActualError(Exception):
    pass

class ActualSplitTransactionError(ActualError):
    pass

class ConditionType(Enum):
    IS = 'is'
    IS_APPROX = 'isapprox'
    GT = 'gt'
    GTE = 'gte'
    LT = 'lt'
    LTE = 'lte'
    CONTAINS = 'contains'
    ONE_OF = 'oneOf'
    IS_NOT = 'isNot'
    DOES_NOT_CONTAIN = 'doesNotContain'
    NOT_ONE_OF = 'notOneOf'
    IS_BETWEEN = 'isbetween'
    MATCHES = 'matches'
    HAS_TAGS = 'hasTags'

class ActionType(Enum):
    SET = 'set'
    SET_SPLIT_AMOUNT = 'set-split-amount'
    LINK_SCHEDULE = 'link-schedule'
    PREPEND_NOTES = 'prepend-notes'
    APPEND_NOTES = 'append-notes'

class ValueType(Enum):
    DATE = 'date'
    ID = 'id'
    STRING = 'string'
    NUMBER = 'number'
    BOOLEAN = 'boolean'
    IMPORTED_PAYEE = 'imported_payee'

    def is_valid(self, operation: ConditionType) -> bool:
        if self == ValueType.DATE:
            return operation.value in ("is", "isapprox", "gt", "gte", "lt", "lte")
        elif self in (ValueType.STRING, ValueType.IMPORTED_PAYEE):
            return operation.value in (
                "is",
                "contains",
                "oneOf",
                "isNot",
                "doesNotContain",
                "notOneOf",
                "matches",
                "hasTags",
            )
        elif self == ValueType.ID:
            return operation.value in ("is", "isNot", "oneOf", "notOneOf")
        elif self == ValueType.NUMBER:
            return operation.value in ("is", "isapprox", "isbetween", "gt", "gte", "lt", "lte")
        else:
            # must be BOOLEAN
            return operation.value in ("is",)

    def validate(self, value: Union[int, List[str], str, None], operation: ConditionType = None) -> bool:
        if isinstance(value, list) and operation in (ConditionType.ONE_OF, ConditionType.NOT_ONE_OF):
            return all(self.validate(v, None) for v in value)
        if value is None:
            return True
        if self == ValueType.ID:
            return isinstance(value, str) and is_uuid(value)
        elif self in (ValueType.STRING, ValueType.IMPORTED_PAYEE):
            return isinstance(value, str)
        elif self == ValueType.DATE:
            try:
                bool(get_value(value, self))
                return True
            except ValueError:
                return False
        elif self == ValueType.NUMBER:
            # Assuming BetweenValue is a class for range checks, not implemented here for simplicity
            # if operation == ConditionType.IS_BETWEEN:
            #     return isinstance(value, BetweenValue)
            # else:
            return isinstance(value, int) or isinstance(value, float)
        else:
            # must be BOOLEAN
            return isinstance(value, bool)

    @classmethod
    def from_field(cls, field: str | None) -> "ValueType":
        if field in ("acct", "category"):
            return ValueType.ID
        elif field in ("notes", "description"):
            return ValueType.STRING
        elif field in ("imported_description",):
            return ValueType.IMPORTED_PAYEE
        elif field in ("date",):
            return ValueType.DATE
        elif field in ("cleared", "reconciled"):
            return ValueType.BOOLEAN
        elif field in ("amount", "amount_inflow", "amount_outflow"):
            return ValueType.NUMBER
        else:
            raise ValueError(f"Field '{field}' does not have a matching ValueType.")

class BetweenValue(BaseModel):
    # Placeholder for BetweenValue if needed for IS_BETWEEN condition
    min: Union[int, float]
    max: Union[int, float]

class Schedule(BaseModel):
    id: str = Field(..., description="The ID of the schedule.")
    name: str = Field(..., description="The name of the schedule.")

class Condition(BaseModel):
    field: Literal['imported_description', 'acct', 'category', 'date', 'description', 'notes', 'amount', 'amount_inflow', 'amount_outflow']
    op: ConditionType
    value: Union[int, float, str, List[str], 'BetweenValue', date, None]
    type: Optional[ValueType] = None
    options: Optional[dict] = None

    @model_validator(mode="after")
    def convert_value(self):
        if self.field in ("amount_inflow", "amount_outflow") and self.options is None:
            self.options = {self.field.split("_")[1]: True}
            self.value = abs(self.value) if isinstance(self.value, (int, float)) else self.value
            self.field = "amount"
        if isinstance(self.value, float):
            self.value = int(self.value * 100)
        return self

    @model_validator(mode="after")
    def check_operation_type(self):
        if not self.type:
            self.type = ValueType.from_field(self.field)
        if not self.type.is_valid(self.op):
            raise ValueError(f"Operation {self.op} not supported for type {self.type}")
        if isinstance(self.value, BaseModel) and hasattr(self.value, "id"):
            self.value = str(self.value.id)
        elif isinstance(self.value, list) and len(self.value) and isinstance(self.value[0], BaseModel):
            self.value = [str(v.id) if hasattr(v, "id") else v for v in self.value]
        if not self.type.validate(self.value, self.op):
            raise ValueError(f"Value {self.value} is not valid for type {self.type.name} and operation {self.op.name}")
        return self

    def __str__(self) -> str:
        v = f"'{self.value}'" if isinstance(self.value, str) else str(self.value)
        return f"'{self.field}' {self.op.value} {v}"

    def as_dict(self):
        ret = self.model_dump(mode="json")
        if not self.options:
            ret.pop("options", None)
        return ret

    def get_value(self) -> Union[int, date, List[str], str, None]:
        return get_value(self.value, self.type)

    def run(self, transaction) -> bool:
        # Placeholder for transaction object, assuming it has attributes like 'notes', 'amount', etc.
        # This needs to be properly integrated with the Actual.py transaction object.
        # For now, a simplified version.
        true_value = getattr(transaction, self.field, None)
        self_value = self.get_value()

        # Simplified condition evaluation
        if self.op == ConditionType.IS:
            return true_value == self_value
        elif self.op == ConditionType.CONTAINS:
            return isinstance(true_value, str) and isinstance(self_value, str) and self_value in true_value
        elif self.op == ConditionType.GT:
            return true_value > self_value
        elif self.op == ConditionType.LT:
            return true_value < self_value
        # Add more condition types as needed based on actualpy's implementation
        return False # Default for unsupported operations

class Action(BaseModel):
    field: Optional[Literal['category', 'description', 'notes', 'cleared', 'acct', 'date', 'amount']] = None
    op: ActionType = Field(ActionType.SET, description="Action type to apply (default changes a column).")
    value: Union[str, bool, int, float, BaseModel, None]
    type: Optional[ValueType] = None
    options: Dict[str, Union[str, int]] = Field(default_factory=dict)
    category_id_to_name_map: Dict[str, str] = {}

    @model_validator(mode="after")
    def convert_value(self):
        if isinstance(self.value, float):
            self.value = int(self.value * 100)
        if self.field in ("cleared",) and self.value in (0, 1):
            self.value = bool(self.value)
        return self

    @model_validator(mode="after")
    def check_operation_type(self):
        if not self.type:
            if self.field is not None:
                self.type = ValueType.from_field(self.field)
            elif self.op == ActionType.LINK_SCHEDULE:
                self.type = ValueType.ID
            elif self.op == ActionType.SET_SPLIT_AMOUNT:
                self.type = ValueType.NUMBER
        if self.op in (ActionType.APPEND_NOTES, ActionType.PREPEND_NOTES):
            self.type = ValueType.STRING
        if isinstance(self.value, BaseModel) and hasattr(self.value, "id"):
            self.value = str(self.value.id)
        if not self.type.validate(self.value):
            raise ValueError(f"Value {self.value} is not valid for type {self.type.name}")
        return self

    def __str__(self) -> str:
        display_value = str(self.value)
        if self.field == 'category' and self.value in self.category_id_to_name_map:
            display_value = f"'{self.category_id_to_name_map[self.value]}'"
        elif isinstance(self.value, str):
            display_value = f"'{self.value}'"

        if self.op in (ActionType.SET, ActionType.LINK_SCHEDULE):
            split_info = ""
            if self.options and self.options.get("splitIndex") > 0:
                split_info = f" at Split {self.options.get('splitIndex')}"
            field_str = f" '{self.field}'" if self.field else ""
            return f"{self.op.value}{field_str}{split_info} to {display_value}"
        elif self.op == ActionType.SET_SPLIT_AMOUNT:
            method = self.options.get("method") or ""
            split_index = self.options.get("splitIndex") or ""
            return f"allocate a {method} at Split {split_index}: {display_value}"
        elif self.op in (ActionType.APPEND_NOTES, ActionType.PREPEND_NOTES):
            return (
                f"append to notes {display_value}"
                if self.op == ActionType.APPEND_NOTES
                else f"prepend to notes {display_value}"
            )
        return "Unknown Action"

    def as_dict(self):
        ret = self.model_dump(mode="json")
        if not self.options:
            ret.pop("options", None)
        return ret

    def run(self, transaction) -> None:
        # This needs to be properly integrated with the Actual.py transaction object.
        # For now, a simplified version.
        if self.op == ActionType.SET:
            if self.field:
                setattr(transaction, self.field, self.value)
        elif self.op == ActionType.APPEND_NOTES:
            current_notes = getattr(transaction, 'notes', '') or ''
            if not current_notes.endswith(self.value):
                transaction.notes = f"{current_notes}{self.value}"
        elif self.op == ActionType.PREPEND_NOTES:
            current_notes = getattr(transaction, 'notes', '') or ''
            if not current_notes.startswith(self.value):
                transaction.notes = f"{self.value}{current_notes}"
        # SET_SPLIT_AMOUNT and LINK_SCHEDULE are more complex and will require actualpy integration
        # For now, they are not fully implemented in this simplified run method.

class Rule(BaseModel):
    conditions: List[Condition] = Field(..., description="List of conditions that need to be met (one or all) in order for the actions to be applied.")
    operation: Literal['and', 'or'] = Field("and", description="Operation to apply for the rule evaluation. If 'all' or 'any' need to be evaluated.")
    actions: List[Action] = Field(..., description="List of actions to apply to the transaction.")
    stage: Literal["pre", "post", None] = Field(None, description="Stage in which the rule will be evaluated (default None)")

    @model_validator(mode="before")
    def correct_operation(cls, value):
        if value.get("operation") == "all":
            value["operation"] = "and"
        elif value.get("operation") == "any":
            value["operation"] = "or"
        return value

    def __str__(self) -> str:
        operation_str = "all" if self.operation == "and" else "any"
        conditions_str = f" {self.operation} ".join([str(c) for c in self.conditions])
        actions_str = ", ".join([str(a) for a in self.actions])
        return f"If {operation_str} of these conditions match {conditions_str} then {actions_str}"

    def evaluate(self, transaction) -> bool:
        op_func = any if self.operation == "or" else all
        return op_func(c.run(transaction) for c in self.conditions)

    def run(self, transaction) -> bool:
        if condition_met := self.evaluate(transaction):
            for action in self.actions:
                action.run(transaction)
        return condition_met

class RuleSet(BaseModel):
    rules: List[Rule] = Field(..., description="List of rules to be evaluated on run.")

    def __str__(self):
        return "\n".join([str(r) for r in self.rules])

    def __iter__(self):
        return self.rules.__iter__()

    def add(self, rule: Rule):
        self.rules.append(rule)

    def run(self, transactions: Union[List, 'Transactions'], stage: Literal["all", "pre", "post", None] = "all"):
        # This run method is simplified. The actualpy documentation shows a more complex _run method
        # that handles stages and single/list transactions. For this implementation, we'll assume
        # transactions is a list and run all rules.
        if not isinstance(transactions, list):
            transactions = [transactions]

        for transaction in transactions:
            for rule in self.rules:
                rule.run(transaction)

# --- Rule Persistence Functions ---
RULES_FILE = "rules.json"

def load_rules(category_id_to_name_map: Dict[str, str] = None) -> RuleSet:
    ruleset = RuleSet(rules=[])
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE, 'r') as f:
            data = json.load(f)
            ruleset = RuleSet(**data)
    
    if category_id_to_name_map:
        for rule in ruleset.rules:
            for action in rule.actions:
                action.category_id_to_name_map = category_id_to_name_map
    
    return ruleset

def save_rules(ruleset: RuleSet):
    with open(RULES_FILE, 'w') as f:
        json.dump(ruleset.model_dump(mode="json"), f, indent=4)

RuleSet.model_rebuild()
Rule.model_rebuild()
Condition.model_rebuild()
Action.model_rebuild()
Schedule.model_rebuild()