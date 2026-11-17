"""Small criteria-expression parser shared by profile UI and CLI runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


ExpressionAst = tuple[str, Any]


class CriteriaExpressionError(ValueError):
    """Raised when a criteria expression cannot be parsed or compiled."""


class CriteriaExpressionParser:
    """Parse criteria text using ``&``/``|``/``!`` operators and parentheses."""

    _OPERATORS = {"&", "|", "(", ")", "!"}

    #: Imbrication maximale (parenthèses, négations) : au-delà, refus explicite plutôt que pile épuisée.
    MAX_DEPTH = 64

    def __init__(self, text: str) -> None:
        self._tokens = self._tokenize(text)
        self._index = 0
        self._depth = 0

    @classmethod
    def _tokenize(cls, text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        source = str(text or "")
        current: list[str] = []
        filled = False  # atome en cours non blanc (évite de rejoindre le préfixe à chaque caractère)

        def flush_atom() -> None:
            nonlocal filled
            atom = "".join(current).strip()
            current.clear()
            filled = False
            if atom:
                tokens.append(("ATOM", atom))

        def add(text: str) -> None:
            nonlocal filled
            current.append(text)
            filled = filled or not text.isspace()

        cursor = 0
        while cursor < len(source):
            char = source[cursor]
            if char == "\\":
                if cursor + 1 >= len(source):
                    raise CriteriaExpressionError("echappement final")
                add(source[cursor + 1])
                cursor += 2
                continue
            if (
                not filled
                and source[cursor:cursor + 3].lower() == "not"
                and cursor + 3 < len(source)
                and source[cursor + 3] == "("
            ):
                flush_atom()
                tokens.append(("!", "!"))
                cursor += 3
                continue
            if char == "!":
                flush_atom()
                if cursor + 1 >= len(source) or source[cursor + 1].isspace():
                    raise CriteriaExpressionError("valeur attendue apres !")
                tokens.append(("!", "!"))
                cursor += 1
                continue
            if char in cls._OPERATORS:
                flush_atom()
                tokens.append((char, char))
                cursor += 1
                continue
            if char in {"'", '"'}:
                quote = char
                cursor += 1
                quoted: list[str] = []
                while cursor < len(source):
                    char = source[cursor]
                    if char == "\\":
                        if cursor + 1 >= len(source):
                            raise CriteriaExpressionError("echappement final")
                        quoted.append(source[cursor + 1])
                        cursor += 2
                        continue
                    if char == quote:
                        cursor += 1
                        break
                    quoted.append(char)
                    cursor += 1
                else:
                    raise CriteriaExpressionError("guillemet non ferme")
                text = "".join(quoted)
                if not text.strip() and not current:
                    raise CriteriaExpressionError("valeur vide")
                if text:
                    add(text)
                continue
            add(char)
            cursor += 1
        flush_atom()
        if not tokens:
            raise CriteriaExpressionError("expression vide")
        return tokens

    def parse(self) -> ExpressionAst:
        node = self._parse_or()
        if self._peek() is not None:
            raise CriteriaExpressionError("operateur ou parenthese inattendue")
        return node

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _consume(self, expected: str | None = None) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise CriteriaExpressionError("fin d'expression inattendue")
        if expected is not None and token[0] != expected:
            raise CriteriaExpressionError(f"{expected} attendu")
        self._index += 1
        return token

    def _parse_or(self) -> ExpressionAst:
        nodes = [self._parse_and()]
        while (token := self._peek()) is not None and token[0] == "|":
            self._consume("|")
            nodes.append(self._parse_and())
        return nodes[0] if len(nodes) == 1 else ("any", nodes)

    def _parse_and(self) -> ExpressionAst:
        nodes = [self._parse_not()]
        while (token := self._peek()) is not None and token[0] == "&":
            self._consume("&")
            nodes.append(self._parse_not())
        return nodes[0] if len(nodes) == 1 else ("all", nodes)

    def _parse_not(self) -> ExpressionAst:
        token = self._peek()
        if token is not None and token[0] == "!":
            self._consume("!")
            return ("not", self._nested(self._parse_not))
        return self._parse_primary()

    def _nested(self, parse: Callable[[], ExpressionAst]) -> ExpressionAst:
        self._depth += 1
        if self._depth > self.MAX_DEPTH:
            raise CriteriaExpressionError(f"expression trop imbriquee (plus de {self.MAX_DEPTH} niveaux)")
        try:
            return parse()
        finally:
            self._depth -= 1

    def _parse_primary(self) -> ExpressionAst:
        token = self._peek()
        if token is None:
            raise CriteriaExpressionError("valeur attendue")
        if token[0] == "ATOM":
            atom = self._consume("ATOM")[1]
            if atom.strip().lower() == "not":
                raise CriteriaExpressionError("NOT attend des parentheses")
            return ("leaf", atom)
        if token[0] == "(":
            self._consume("(")
            node = self._nested(self._parse_or)
            self._consume(")")
            return node
        raise CriteriaExpressionError("valeur attendue")


def parse_criteria_expression(text: str) -> ExpressionAst:
    return CriteriaExpressionParser(text).parse()


def compile_criteria_expression(
    text: str,
    atom_builder: Callable[[str], Mapping[str, Any] | None],
) -> dict[str, Any]:
    return criteria_ast_condition(parse_criteria_expression(text), atom_builder)


def criteria_ast_condition(
    ast: ExpressionAst,
    atom_builder: Callable[[str], Mapping[str, Any] | None],
) -> dict[str, Any]:
    kind, value = ast
    if kind == "leaf":
        condition = atom_builder(str(value))
        if not isinstance(condition, Mapping):
            raise CriteriaExpressionError(f"critere inconnu : {value}")
        return dict(condition)
    if kind in {"all", "any"}:
        children = [criteria_ast_condition(child, atom_builder) for child in value]
        flattened: list[dict[str, Any]] = []
        for child in children:
            if set(child.keys()) == {kind} and isinstance(child.get(kind), list):
                flattened.extend(child[kind])
            else:
                flattened.append(child)
        return {kind: flattened}
    if kind == "not":
        return {"not": criteria_ast_condition(value, atom_builder)}
    raise CriteriaExpressionError("expression invalide")
