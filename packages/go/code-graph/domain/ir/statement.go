package ir

type StatementKind string

const (
	StatementBlock        StatementKind = "block"
	StatementExpression   StatementKind = "expression"
	StatementDeclaration  StatementKind = "declaration"
	StatementReturn       StatementKind = "return"
	StatementYield        StatementKind = "yield"
	StatementThrow        StatementKind = "throw"
	StatementBreak        StatementKind = "break"
	StatementContinue     StatementKind = "continue"
	StatementIf           StatementKind = "if"
	StatementWhile        StatementKind = "while"
	StatementDo           StatementKind = "do"
	StatementFor          StatementKind = "for"
	StatementEnhancedFor  StatementKind = "enhanced_for"
	StatementTry          StatementKind = "try"
	StatementCatch        StatementKind = "catch"
	StatementSynchronized StatementKind = "synchronized"
	StatementAssert       StatementKind = "assert"
	StatementLabeled      StatementKind = "labeled"
	StatementSwitch       StatementKind = "switch"
	StatementSwitchArm    StatementKind = "switch_arm"
	StatementSwitchLabel  StatementKind = "switch_label"
	StatementEmpty        StatementKind = "empty"
	StatementUnknown      StatementKind = "unknown"
)

// Statement is syntax for later flow/target-type analysis, not a computed CFG.
// Children preserve order within blocks/arms. Roles on control statements are
// explicit so return/yield and loop headers cannot be confused with side effects.
// Labels stay as names; a binder checks/links jump targets. A syntax tree and
// expression operators provide the inputs for pattern visibility and completion
// analysis, including short circuiting and early exits. Neither is guessed here.
type Statement struct {
	ID                     StatementID     `json:"id"`
	Kind                   StatementKind   `json:"kind"`
	Span                   Span            `json:"span"`
	ScopeID                ScopeID         `json:"scope_id"`
	EnclosingDeclarationID DeclarationID   `json:"enclosing_declaration_id,omitempty"`
	ChildIDs               []StatementID   `json:"child_ids,omitempty"`
	DeclarationIDs         []DeclarationID `json:"declaration_ids,omitempty"`
	ExpressionIDs          []ExpressionID  `json:"expression_ids,omitempty"`
	ConditionID            ExpressionID    `json:"condition_id,omitempty"`
	BodyID                 StatementID     `json:"body_id,omitempty"`
	AlternativeID          StatementID     `json:"alternative_id,omitempty"`
	InitializerIDs         []StatementID   `json:"initializer_ids,omitempty"`
	UpdateIDs              []ExpressionID  `json:"update_ids,omitempty"`
	ResourceIDs            []StatementID   `json:"resource_ids,omitempty"`
	CatchIDs               []StatementID   `json:"catch_ids,omitempty"`
	FinallyID              StatementID     `json:"finally_id,omitempty"`
	PatternIDs             []PatternID     `json:"pattern_ids,omitempty"`
	Label                  *Name           `json:"label,omitempty"`
	Default                bool            `json:"default,omitempty"`
	Arrow                  bool            `json:"arrow,omitempty"`
	SyntaxKind             string          `json:"syntax_kind,omitempty"`
}

type PatternKind string

const (
	PatternType    PatternKind = "type"
	PatternRecord  PatternKind = "record"
	PatternUnnamed PatternKind = "unnamed"
	PatternUnknown PatternKind = "unknown"
)

// A pattern variable's lexical anchor is its containing scope. It is NOT an
// ordinary local visible throughout that scope. Only successful-match flow
// established from PatternID/statement/boolean-expression links makes it visible.
// Components preserve record deconstruction order. Types remain unresolved.
type Pattern struct {
	ID                     PatternID     `json:"id"`
	Kind                   PatternKind   `json:"kind"`
	Span                   Span          `json:"span"`
	ScopeID                ScopeID       `json:"scope_id"`
	EnclosingDeclarationID DeclarationID `json:"enclosing_declaration_id,omitempty"`
	TypeRefID              TypeRefID     `json:"type_ref_id,omitempty"`
	DeclarationID          DeclarationID `json:"declaration_id,omitempty"`
	ComponentIDs           []PatternID   `json:"component_ids,omitempty"`
}
