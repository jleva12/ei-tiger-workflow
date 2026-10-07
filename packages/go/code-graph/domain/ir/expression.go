package ir

import "unicode/utf8"

type ExpressionKind string

const (
	// ExpressionPython retains Python operand structure (await, yield,
	// collections, comprehensions, slices and unpacking), tagged by SyntaxKind.
	ExpressionPython            ExpressionKind = "python_expression"
	ExpressionName              ExpressionKind = "name"
	ExpressionLiteral           ExpressionKind = "literal"
	ExpressionThis              ExpressionKind = "this"
	ExpressionSuper             ExpressionKind = "super"
	ExpressionMemberAccess      ExpressionKind = "member_access"
	ExpressionCall              ExpressionKind = "call"
	ExpressionCallableReference ExpressionKind = "callable_reference"
	ExpressionParenthesized     ExpressionKind = "parenthesized"
	ExpressionCast              ExpressionKind = "cast"
	ExpressionArrayAccess       ExpressionKind = "array_access"
	ExpressionArrayCreation     ExpressionKind = "array_creation"
	ExpressionArrayInitializer  ExpressionKind = "array_initializer"
	ExpressionAssignment        ExpressionKind = "assignment"
	ExpressionUnary             ExpressionKind = "unary"
	ExpressionBinary            ExpressionKind = "binary"
	ExpressionConditional       ExpressionKind = "conditional"
	ExpressionLambda            ExpressionKind = "lambda"
	ExpressionClassLiteral      ExpressionKind = "class_literal"
	ExpressionInstanceOf        ExpressionKind = "instanceof"
	ExpressionSwitch            ExpressionKind = "switch"
	// ExpressionObjectLiteral is an object literal; its operands are the
	// property values, spread operands and function-valued members.
	ExpressionObjectLiteral ExpressionKind = "object_literal"
	// ExpressionMarkup is an embedded markup element (JSX); its operands are
	// the attribute and child expressions. The element a capitalized or
	// qualified tag names is a separate name or member reference.
	ExpressionMarkup  ExpressionKind = "markup"
	ExpressionUnknown ExpressionKind = "unknown"
)

// Expression forms a file-local syntax graph. OperandIDs are source ordered;
// member access stores its qualifier as the first operand. Parenthesized/cast
// expressions have one operand; array access has array then index; conditional
// expressions have condition, true, false. Call/reference details live in the
// matching occurrence table identified by OccurrenceID.
//
// Spelling is exact source text. Unsupported syntax is ExpressionUnknown with
// SyntaxKind and a coverage issue, rather than being reduced to a leaf name.
// This is an expression model for dependency extraction, not a complete AST/CFG.
// MaxExpressionSpelling bounds the source text an expression keeps. An
// enclosing expression repeats its operands' text, so a long chain (a
// generated concatenation of thousands of strings) would otherwise grow the
// IR with the square of its length; the span still addresses all of it.
const MaxExpressionSpelling = 256

// BoundSpelling is an expression's spelling within MaxExpressionSpelling:
// the text itself, or its first bytes on a character boundary and "…".
func BoundSpelling(text string) string {
	if len(text) <= MaxExpressionSpelling {
		return text
	}
	cut := MaxExpressionSpelling - len("…")
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return text[:cut] + "…"
}

type Expression struct {
	ID           ExpressionID   `json:"id"`
	Kind         ExpressionKind `json:"kind"`
	Span         Span           `json:"span"`
	ScopeID      ScopeID        `json:"scope_id"`
	Spelling     string         `json:"spelling"`
	SyntaxKind   string         `json:"syntax_kind,omitempty"`
	Name         *Name          `json:"name,omitempty"`
	Operator     string         `json:"operator,omitempty"`
	OperandIDs   []ExpressionID `json:"operand_ids,omitempty"`
	TypeRefID    TypeRefID      `json:"type_ref_id,omitempty"`
	OccurrenceID OccurrenceID   `json:"occurrence_id,omitempty"`
	Literal      *Literal       `json:"literal,omitempty"`
	Lambda       *Lambda        `json:"lambda,omitempty"`
	PatternID    PatternID      `json:"pattern_id,omitempty"`
	Switch       *Switch        `json:"switch,omitempty"`
}

type LiteralKind string

const (
	LiteralString    LiteralKind = "string"
	LiteralCharacter LiteralKind = "character"
	LiteralInteger   LiteralKind = "integer"
	LiteralFloating  LiteralKind = "floating"
	LiteralBoolean   LiteralKind = "boolean"
	LiteralNull      LiteralKind = "null"
)

// Lexeme preserves radix, suffixes, escapes, precision, and explicit false/null.
// Do not decode Java numeric values through a generic JSON float64.
type Literal struct {
	Kind   LiteralKind `json:"kind"`
	Lexeme string      `json:"lexeme"`
}

type Lambda struct {
	ScopeID          ScopeID         `json:"scope_id"`
	ParameterIDs     []DeclarationID `json:"parameter_ids,omitempty"`
	BodyExpressionID ExpressionID    `json:"body_expression_id,omitempty"`
	BodyScopeID      ScopeID         `json:"body_scope_id,omitempty"`
	BodyStatementID  StatementID     `json:"body_statement_id,omitempty"`
}

// Switch retains result/label/guard structure without deciding result typing
// or exhaustiveness. Its arms are StatementSwitchArm nodes in the body block.
type Switch struct {
	BodyStatementID StatementID `json:"body_statement_id"`
}
