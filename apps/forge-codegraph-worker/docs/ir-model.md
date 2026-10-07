# Post-parse IR contract

Schema version: `1.1.0`. Entry type: [SourceFile](../../../packages/go/code-graph/domain/ir/file.go).

This is a Java-oriented foundation with reusable language/source identifiers. Defining a construct in the model does not establish parser support for a Java version or another language. The [Java Tree-sitter adapter](../internal/parser/java/README.md) documents its extraction feature set and release profiles. Binding is not part of this model: the [javac resolver](../internal/resolve/java/README.md) proves targets and writes them as `pkg/semantic` symbols and lookups that reference these file-local IDs.

## File envelope and identities

`SourceFile` contains source/producer identity, optional package/module syntax, language-validation status, a root scope, typed tables, diagnostics, and extraction coverage. It can be serialized with standard `encoding/json`. All core semantics have typed fields; there is no generic metadata bag whose types change after a JSON round trip.

| Identity | Meaning |
| --- | --- |
| `Source.FileID` | File identity in its repository snapshot. |
| Repository/snapshot IDs and content SHA-256 | The exact source and revision represented by the records. |
| Build context/module/source set/artifact IDs | Optional references to separately stored inventories. Empty means unknown/unavailable, not an inferred default. |
| `DeclarationID` | A declaration within this file snapshot. It is not a stable cross-snapshot Java signature. |
| `ScopeID`, `TypeRefID`, `ExpressionID`, `AnnotationID`, `StatementID`, `PatternID` | Links within their respective file-local tables. |
| `OccurrenceID` | One source occurrence, unique across imports, calls, callable references, value references, type uses, annotation occurrences, and module directives. |

IDs are opaque. Producers allocate them. The Java adapter uses deterministic file-local syntax/traversal IDs; the shared contract does not require a particular allocation algorithm. Identical ID text in different typed namespaces is permitted. Do not reuse an occurrence ID for two calls, including calls on the same line. Stable canonical declaration keys and artifact identities are assigned by the resolver (`semantic.DeclarationKey`, `semantic.ExternalSymbol`), never by the parser.

## Source positions

Spans are half-open `[start, end)` over the original UTF-8 bytes. Byte offsets and byte columns are zero-based; lines are one-based. CRLF must be measured without newline normalization. Editor UTF-16 columns require an explicit adapter. The Java frontend translates Unicode escapes before native parsing, then maps spans to the original bytes. Names, primitive names and operators use translated token text; `Spelling`, `Literal.Lexeme` and documentation retain original bytes. Thus `class \u0050` declares the name `P` but its name span selects the escape spelling.

A zero offset is a real position. Optional locations such as a missing declaration name use `*Span == nil`. Source positions, file byte size, and exact expression spellings are checked against a fixture containing a multibyte character.

## Tables

| Table | Important fields and semantics |
| --- | --- |
| Scopes | Kind, parent, owning declaration, span, and optional disjoint syntax regions. This is syntactic containment, not the result of Java flow-sensitive binding. |
| Declarations | Kind/name, declaring scope, body scope, owner, spans, explicit modifiers/annotations, signature text, and one detail payload. |
| Imports | Full written name/spelling and explicit kind: single type, on-demand type, single static, on-demand static, module, or re-export. An unresolved explicit import remains a fact. `Module` carries the written module specifier for languages that import from module paths (TypeScript); a `re_export` forwards a binding of that module (or, with no module, of the file itself) under `Alias`. |
| Types | Written name segments, generic arguments per segment, arrays/dimension annotations, bounds, unions/intersections, primitive/void/inferred/unknown syntax. Function, structural (object literal, tuple, mapped), literal and operator (`keyof`, `typeof`, indexed, conditional) types name no declaration; the written types inside them are separate type uses with the `component` role and the containing type as parent. |
| Expressions | Kind, scope/span/spelling, source-ordered operands, operator/type syntax, and distinct literal/lambda or occurrence details. Object literals (`object_literal`) carry their property values as operands; markup (`markup`, JSX) carries its attribute and child expressions, and the component a tag names is a separate reference. |
| Calls | Invocation kind, whole receiver expression, explicit type arguments, ordered argument expressions, constructed type when written, and optional anonymous type ownership. |
| Callable references | Method/constructor reference form, qualifier expression, name, and explicit type arguments. There is no argument-count field. |
| References | Value/name/member occurrences with receiver and read/write/read-write/unknown access. Whether a name denotes a field is decided later. |
| Type uses | Type occurrence role, enclosing declaration, and optional enclosing written type. Generic field arguments remain attributable to the field. |
| Annotations | Written annotation type, named or shorthand arguments, arrays, nested annotations, and expression-valued elements. |
| Statements | Ordered block children, return/yield/throw expressions, branches, loop headers, try/catch/finally/resources, labels and switch arms/labels/guards. |
| Patterns | Type/record/unnamed/unknown kind, written type, pattern-variable declaration and ordered component links. |
| Module | Name/open flag/annotations and requires/exports/opens/uses/provides directives with exact occurrences and typed dependencies. |
| Language validation | Separate `not_checked`/`invalid` status, method and release; never inferred from extraction completeness. |
| Diagnostics/coverage | Codes, severity, feature/range, partial/failed status, and explicit extraction losses with known or unknown counts. |

### Declaration details

Each declaration uses one detail payload:

| Kinds | Payload |
| --- | --- |
| Class, interface, enum, record, annotation type, type alias, namespace | `TypeDeclaration`: top-level/member/local/anonymous form, type parameters, record components, written heritage. |
| Method, function, constructor | `CallableDeclaration`: parameter/type-parameter IDs, return/throws types, constructor form, body statement/expression links and an optional annotation-default value. |
| Field, module-level variable, local variable, parameter, receiver parameter, record component, pattern variable, enum constant | `VariableDeclaration`: written type, initializer expression, variadic syntax. |
| Type parameter | Written bounds. |
| Static/instance initializer block | Explicit initializer kind plus its declaration-owned body scope and statement block. |

Named and anonymous/local types have real ownership. Constructor declarations are not ordinary methods. A compact record constructor has an explicit constructor form; do not fabricate its implicit parameter declarations during syntax extraction.

A declaration's `DeclaringScopeID` identifies where it is introduced. Its `BodyScopeID` identifies the scope it introduces for parameters/members/body. A callable scope can include the signature and body so parameter/type-parameter syntax is inside it; nested statement blocks can introduce additional scopes. Source parameter ordering is stored in the callable's parameter list.

Field initializer expressions are attached to their Field declaration through `Variable.InitializerID`; an initializer scope may be owned by that field. Static/instance block initializers use separate declarations. Preserve this distinction for future execution/impact modeling.

Modifiers are explicit written keywords. Implicit interface/public/static or generated record/Lombok members are semantic/enrichment work. When a build produces actual generated source files, those files can have their own source identities.

### Types and expressions

`a.Outer<T>.Inner<U>` stores all three name segments, with T on Outer and U on Inner. `TypeSegment.ArgumentSyntax` is absent for no written argument list, `explicit` for a nonempty list and `diamond` for `<>`; inferred substitutions are not stored here. The segments do not yet certify which names are packages or types. A written T is a named type occurrence until binding identifies its type-parameter declaration.

Type references are syntax occurrences rather than interned semantic types. Separate uses of T or String can have different scopes and spans. TypeUse records connect these written types to fields, parameters, generic arguments, throws clauses, annotations, callable-reference qualifiers, and other declaration contexts. Post-name array suffix records retain their own exact fragment spans/spellings and link the leading type through `ElementTypeID`. Variadic parameters and record components use `Variable.Variadic`.

Expression operand conventions are documented on `Expression`. A call expression references its Call occurrence; that Call points to its receiver and arguments. Thus `a.next().save()` retains the inner call as the outer receiver. The linked statement table supplies structured flow inputs. It is not a computed control-flow graph. Unsupported constructs require diagnostics/coverage and can retain unknown-expression syntax.

Literal values keep exact source lexemes. Integer suffixes/radix, floating-point spellings, false, null, and string escapes are not converted through `map[string]any` or float64.

### Statements, patterns and executable bodies

`Callable.BodyStatementID` and `Initializer.BodyStatementID` anchor executable syntax. A block lambda links both `BodyScopeID` and `BodyStatementID`; an expression lambda uses `BodyExpressionID`. Traverse each executable body independently, stopping at nested lambda/type/callable boundaries when analyzing returns. Branches use `ConditionID`, `BodyID` and `AlternativeID`. Loops retain initializer statements, updates and their body. Try statements retain resources, catches and finally. Return/yield/throw statements link their result expression; expression statements remain distinct. Labels preserve written names; jump targets require analysis.

Switch expressions retain the selector operand and a statement block of ordered arms. Each arm records arrow versus colon syntax; label children retain constant expressions, patterns, guard conditions and the default flag. `case null, default` keeps both the null expression and default. An arrow expression child is an implicit result; a block arm uses explicit yield statements. Source order preserves potential fallthrough, without computing reachability or exhaustiveness.

`DeclarationPatternVariable` must not be indexed as an ordinary local visible throughout its declaring scope. Its `Pattern` supplies the match condition; type patterns link declarations, record patterns link ordered components. The containing scope is only a lexical anchor. A binder derives visibility from the linked boolean/branch/loop/abrupt-completion syntax, including `!`, `&&`, `||`, and early exits. No flow intervals are fabricated at parse time. These rules follow the distinction between syntax inputs and computed scope in [JLS 6.3](https://docs.oracle.com/javase/specs/jls/se21/html/jls-6.html#jls-6.3).

Field/local declarations use individual declarator spans and signature fragments (`A=1`, `B=2`), individual name spans and initializer links. Shared written types/modifiers/annotations have their own spans, which may precede the declarator span. A callable/type signature is its raw declaration prefix before the body, with surrounding whitespace trimmed; it is not a canonical semantic signature. Adjacent field-group Javadocs are copied to each field owner. Documentation retains the exact block comment; it does not include intervening whitespace or an ordinary block comment.

### Calls and callable references

| Syntax | Record |
| --- | --- |
| `service.save(x)` | Method Call with a name, receiver ExpressionID, and ordered arguments. |
| `new Client(x)` | Object-creation Call with a written constructed TypeRefID. No selected constructor ID yet. |
| `this(x)` / `super(x)` | Explicit constructor-delegation Call kinds. |
| Enum constant creation | Its own Call kind; implicit owner/type facts can be derived later. |
| `this::accept` | CallableReference to a method name and qualifier expression. |
| `Client::new` | Constructor CallableReference preserving the name `new`. |

Call/callable-reference records and their associated expressions agree on occurrence ID, expression kind, scope, and full span. Call arguments are a slice in source order; there is no duplicate mutable argument index. The binder supplies target types/signatures/ambiguity later.

### Annotation values

An AnnotationValue has one kind: expression, array, or nested annotation. Arrays retain every element, including the distinction between an empty array and an expression-valued null. Argument names remain available, so `path` and `produces` cannot become interchangeable string literals. Shorthand annotation arguments use an empty argument name rather than inventing a shared-language default. Annotation-element defaults use the same `AnnotationValue` union in `Callable.AnnotationDefault`. They never compete with the legacy `DefaultValueID` expression slot.

### Validation and serialization

Call `file.Validate()` before publishing parsed facts. It checks schema/source envelope, IDs, local references, source ranges, root/parent scopes, ownership cycles, expression/type and cross-table statement/expression/pattern cycles, main tagged payloads, call-expression agreement, and coverage consistency. It returns joined `ValidationError` values with field paths.

Validation is structural. It does not read the source file, authenticate its hash, check all grammar-specific role/operand constraints, resolve names, or prove extraction completeness. Callers handling serialized external input must impose size/depth limits before decoding; the validator is intended for locally produced model records. Its annotation-value traversal has a limit of 256 levels and reports exceeding it.

Use `json.Decoder.DisallowUnknownFields()` when a consumer needs strict field compatibility, then validate the schema version. Unknown versions require an explicit migration/decoder change. Do not silently accept a new version or mix incompatible parser and binder outputs.

The complete/partial/failed extraction status is relative to `Coverage.FeatureSet` and producer version. Complete extraction is not a promise of complete Java/runtime dependency discovery. Partial/failed extraction includes coverage issues. Nil Count means unknown loss, while a pointer to zero means a known zero.

The hand-authored [fixture](../../../packages/go/code-graph/domain/ir/testdata/parsed_file.json) is marked partial and has no resolved edges. [Contract tests](../../../packages/go/code-graph/domain/ir/ir_test.go) verify serialization fidelity and reject common corruptions. The [Java parser tests](../internal/parser/java/parser_test.go) independently assert real extraction facts, ownership, coverage, budgets, cancellation and malformed-input recovery.

Version 1.1.0 requires an explicit decoder update or re-extraction of 1.0.0 artifacts; the strict validator rejects old versions. New Java output declares adapter `0.2.0` and feature set `java-syntax/2`. Pattern and statement IDs and updated Unicode mapping mean old file-local IDs must not be mixed with new extraction output. The graph schema in Spanner is versioned separately.
