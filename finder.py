"""Acha o ranking do evento dentro dos pacotes gravados.

O protocolo nao tem nomes de campo, entao o caminho e descoberto e nao suposto:
o administrador digita 2 ou 3 linhas que esta vendo na tela (posicao, nome,
pontos) e este modulo:

1. procura em cada resposta gravada onde esses nomes e pontos aparecem;
2. compara os caminhos entre jogadores diferentes: o unico indice de lista que
   muda de um jogador para o outro e a linha do ranking. Isso vira um molde,
   por exemplo `$.frames[0].objects[1][*][2]` para o nome;
3. aplica o molde a lista inteira e confere o resultado (posicoes continuas,
   pontos em ordem decrescente, os jogadores digitados batendo).

Nada aqui depende de interface grafica, para poder ser testado e reaproveitado
no app final.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import tbcodec

Path = Tuple[Any, ...]

MAX_NAME_HITS = 60
MAX_NUMBER_HITS = 400


# --------------------------------------------------------------------- basicos
@dataclass
class Target:
    """Uma linha que o administrador leu na tela."""

    name: str
    points: int
    position: Optional[int] = None
    power: Optional[int] = None


def parse_int(text: Any) -> Optional[int]:
    """'2,010,420,872' / '2.010.420.872' / '2010420872 points' -> 2010420872."""
    if text is None:
        return None
    digits = re.sub(r"\D", "", str(text))
    return int(digits) if digits else None


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


_NUMERIC_STR = re.compile(r"^\s*\d{1,3}(?:[.,\s]\d{3})+\s*$|^\s*\d+\s*$")


def as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _NUMERIC_STR.match(value):
        return parse_int(value)
    return None


def name_matches(leaf: str, wanted: str) -> bool:
    """Igual, ou contendo o nome digitado (o pacote pode trazer tag de reino)."""
    have = norm(leaf)
    if have == wanted:
        return True
    return len(wanted) >= 3 and wanted in have and len(have) <= len(wanted) + 16


def walk(node: Any, path: Path = ()) -> Iterator[Tuple[Path, Any]]:
    """Todas as folhas como (caminho, valor). Iterativo: arvores do jogo sao fundas."""
    stack: List[Tuple[Path, Any]] = [(path, node)]
    while stack:
        p, n = stack.pop()
        if isinstance(n, dict):
            for k in reversed(list(n.keys())):
                stack.append((p + (k,), n[k]))
        elif isinstance(n, list):
            for i in range(len(n) - 1, -1, -1):
                stack.append((p + (i,), n[i]))
        else:
            yield p, n


def get_path(tree: Any, path: Path) -> Any:
    node = tree
    for key in path:
        if isinstance(node, dict) and not isinstance(key, int) and key in node:
            node = node[key]
        elif isinstance(node, dict) and str(key) in node:
            node = node[str(key)]
        elif isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        else:
            return None
    return node


def format_path(path: Path, wildcard_at: Optional[int] = None) -> str:
    out = "$"
    for i, key in enumerate(path):
        if i == wildcard_at:
            out += "[*]"
        elif isinstance(key, int):
            out += f"[{key}]"
        else:
            out += f".{key}"
    return out


_tree_cache: Dict[int, Any] = {}


def tree_of(record: dict) -> Any:
    key = id(record)
    if key not in _tree_cache:
        try:
            data = base64.b64decode(record.get("b64", ""))
            _tree_cache[key] = tbcodec.decode_payload(data)
        except Exception:
            _tree_cache[key] = None
    return _tree_cache[key]


def clear_cache() -> None:
    _tree_cache.clear()


# --------------------------------------------------------------------- busca
@dataclass
class TargetHits:
    names: List[Path] = field(default_factory=list)
    points: List[Path] = field(default_factory=list)
    power: List[Path] = field(default_factory=list)


@dataclass
class Candidate:
    record: dict
    hits: List[TargetHits]
    full: int      # jogadores com nome E pontos achados
    partial: int   # jogadores com nome OU pontos achados

    @property
    def routes(self) -> str:
        return ",".join(str(r) for r in dict.fromkeys(self.record.get("routes") or [])) or "-"


def search(records: Sequence[dict], targets: Sequence[Target]) -> List[Candidate]:
    wanted = [norm(t.name) for t in targets]
    candidates: List[Candidate] = []

    for rec in records:
        tree = tree_of(rec)
        if tree is None:
            continue
        hits = [TargetHits() for _ in targets]
        for path, value in walk(tree):
            if isinstance(value, str):
                for i, w in enumerate(wanted):
                    if w and len(hits[i].names) < MAX_NAME_HITS and name_matches(value, w):
                        hits[i].names.append(path)
            number = as_int(value)
            if number is None:
                continue
            for i, t in enumerate(targets):
                if number == t.points and len(hits[i].points) < MAX_NUMBER_HITS:
                    hits[i].points.append(path)
                if t.power is not None and number == t.power and len(hits[i].power) < MAX_NUMBER_HITS:
                    hits[i].power.append(path)

        full = sum(1 for h in hits if h.names and h.points)
        partial = sum(1 for h in hits if h.names or h.points)
        with_points = sum(1 for h in hits if h.points)
        # Pacote so com pontos tambem interessa: o ranking pode vir por id, sem
        # nome, e os nomes em outra resposta.
        if full or any(h.names for h in hits) or with_points >= 2:
            candidates.append(Candidate(rec, hits, full, partial))

    def rank(c: Candidate) -> tuple:
        names = sum(1 for h in c.hits if h.names)
        points = sum(1 for h in c.hits if h.points)
        return (-c.full, -max(names, points), -min(names, points), c.record.get("size", 0))

    candidates.sort(key=rank)
    return candidates


def best_names_and_points(candidates: Sequence[Candidate]) -> Optional[Candidate]:
    """O candidato por onde comecar: nome e pontos juntos, senao o de mais pontos."""
    if not candidates:
        return None
    if candidates[0].full >= 2:
        return candidates[0]
    by_points = max(candidates, key=lambda c: (sum(1 for h in c.hits if h.points), -c.record.get("size", 0)))
    return by_points if sum(1 for h in by_points.hits if h.points) >= 2 else candidates[0]


# ------------------------------------------------------------------- moldes
@dataclass
class Template:
    """Onde cada coluna do ranking mora, com `*` sendo a linha."""

    list_path: Path                 # a lista cujos itens sao as linhas (nome)
    name_suffix: Path
    points_list_path: Path
    points_suffix: Path
    offset: int = 0                 # indice dos pontos = indice do nome + offset
    style: str = "registro"         # registro | listas paralelas
    position_suffix: Optional[Path] = None
    position_index_base: Optional[int] = None   # posicao = indice + base
    power_list_path: Optional[Path] = None
    power_suffix: Optional[Path] = None
    power_offset: int = 0

    def describe(self) -> List[str]:
        n = len(self.list_path)
        lines = [
            f"Estilo: {self.style}",
            f"Nome:   {format_path(self.list_path + (0,) + self.name_suffix, n)}",
            f"Pontos: {format_path(self.points_list_path + (0,) + self.points_suffix, len(self.points_list_path))}"
            + (f"  (indice do nome {self.offset:+d})" if self.offset else ""),
        ]
        if self.position_suffix is not None:
            lines.append(f"Posicao: {format_path(self.list_path + (0,) + self.position_suffix, n)}")
        elif self.position_index_base is not None:
            lines.append(f"Posicao: nao trafega; = indice da linha {self.position_index_base:+d}")
        else:
            lines.append("Posicao: nao identificada (informe a posicao das linhas digitadas)")
        if self.power_list_path is not None:
            lines.append(
                f"Poder:  {format_path(self.power_list_path + (0,) + (self.power_suffix or ()), len(self.power_list_path))}"
            )
        if self.list_path and self.list_path[0] == "frames" and len(self.list_path) > 1:
            lines.append(f"Frame: indice {self.list_path[1]}")
        return lines

    def with_frame(self, frame_index: int) -> "Template":
        def swap(p: Optional[Path]) -> Optional[Path]:
            if p is None or len(p) < 2 or p[0] != "frames":
                return p
            return ("frames", frame_index) + p[2:]
        return replace(
            self,
            list_path=swap(self.list_path),
            points_list_path=swap(self.points_list_path),
            power_list_path=swap(self.power_list_path),
        )

    def to_json(self) -> dict:
        data = asdict(self)
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in data.items()}


def _row_key_type(a: Any, b: Any) -> bool:
    """A chave que muda entre jogadores pode ser indice de lista ou chave de
    mapa (ranking indexado pelo id do jogador)."""
    return type(a) is type(b) and isinstance(a, (int, str)) and not isinstance(a, bool)


def _offset(name_keys: Sequence[Any], other_keys: Sequence[Any]) -> Optional[int]:
    """Relacao entre a linha do nome e a linha de outra coluna, ou None."""
    if all(isinstance(k, int) for k in list(name_keys) + list(other_keys)):
        offsets = {o - n for n, o in zip(name_keys, other_keys)}
        return offsets.pop() if len(offsets) == 1 else None
    if all(isinstance(k, str) for k in list(name_keys) + list(other_keys)):
        return 0 if list(name_keys) == list(other_keys) else None
    return None


def _row_keys(node: Any) -> List[Any]:
    if isinstance(node, list):
        return list(range(len(node)))
    if isinstance(node, dict):
        return list(node.keys())
    return []


def _shift(key: Any, offset: int) -> Any:
    return key + offset if isinstance(key, int) else key


def _single_wildcards(paths_per_target: List[List[Path]]) -> Dict[Tuple[Path, int], List[Any]]:
    """Moldes em que so uma chave (indice ou chave de mapa) muda entre os jogadores.

    Devolve {(caminho, posicao_do_curinga): [chave de cada jogador]}.
    """
    if len(paths_per_target) < 2 or not all(paths_per_target):
        return {}

    first, second = paths_per_target[0], paths_per_target[1]
    by_len: Dict[int, List[Path]] = {}
    for b in second:
        by_len.setdefault(len(b), []).append(b)

    found: Dict[Tuple[Path, int], List[int]] = {}
    for a in first:
        for b in by_len.get(len(a), []):
            diff = [k for k in range(len(a)) if a[k] != b[k]]
            if len(diff) != 1:
                continue
            k = diff[0]
            if not _row_key_type(a[k], b[k]):
                continue
            key = (a[:k] + (None,) + a[k + 1:], k)
            if key in found:
                continue
            indexes = [a[k], b[k]]
            ok = True
            for others in paths_per_target[2:]:
                match = next(
                    (c for c in others
                     if len(c) == len(a) and _row_key_type(c[k], a[k])
                     and all(c[j] == a[j] for j in range(len(a)) if j != k)),
                    None,
                )
                if match is None:
                    ok = False
                    break
                indexes.append(match[k])
            if ok and len(set(indexes)) == len(indexes):
                found[key] = indexes
    return found


def infer_template(tree: Any, targets: Sequence[Target], hits: Sequence[TargetHits]) -> Tuple[Optional[Template], str]:
    usable = [i for i, h in enumerate(hits) if h.names and h.points]
    if len(usable) < 2:
        return None, "Preciso de pelo menos 2 jogadores com nome e pontos achados no mesmo pacote."

    name_tpls = _single_wildcards([hits[i].names for i in usable])
    points_tpls = _single_wildcards([hits[i].points for i in usable])
    if not name_tpls:
        return None, "Os nomes aparecem, mas nao numa mesma lista (confira se digitou jogadores diferentes)."
    if not points_tpls:
        return None, "Os pontos aparecem, mas nao numa mesma lista (confira os numeros digitados)."

    best: Optional[Tuple[tuple, Template]] = None
    for (npath, nk), nidx in name_tpls.items():
        for (ppath, pk), pidx in points_tpls.items():
            offset = _offset(nidx, pidx)
            if offset is None:
                continue
            list_path, name_suffix = npath[:nk], npath[nk + 1:]
            plist, psuffix = ppath[:pk], ppath[pk + 1:]
            record_style = plist == list_path and offset == 0
            common = 0
            for x, y in zip(npath, ppath):
                if x != y:
                    break
                common += 1
            rank = (0 if record_style else 1, -common, len(npath))
            tpl = Template(
                list_path=list_path,
                name_suffix=name_suffix,
                points_list_path=plist,
                points_suffix=psuffix,
                offset=offset,
                style="registro" if record_style else "listas paralelas",
            )
            if best is None or rank < best[0]:
                best = (rank, tpl)

    if best is None:
        return None, "Nomes e pontos estao em listas, mas a ordem das linhas nao bate entre elas."

    tpl = best[1]
    _infer_position(tree, tpl, [targets[i] for i in usable], _name_indexes(tpl, name_tpls))
    _infer_power(tpl, targets, hits, usable, _name_indexes(tpl, name_tpls))
    return tpl, "ok"


def _name_indexes(tpl: Template, name_tpls: Dict[Tuple[Path, int], List[Any]]) -> List[Any]:
    key = (tpl.list_path + (None,) + tpl.name_suffix, len(tpl.list_path))
    return name_tpls[key]


def _infer_position(tree: Any, tpl: Template, targets: List[Target], keys: List[Any]) -> None:
    known = [(t.position, key) for t, key in zip(targets, keys) if t.position is not None]
    if not known:
        return

    rows_node = get_path(tree, tpl.list_path)
    if tpl.style == "registro":
        candidates: Optional[set] = None
        for position, key in known:
            row = get_path(tree, tpl.list_path + (key,))
            suffixes = {p for p, v in walk(row) if not isinstance(v, str) and as_int(v) == position}
            suffixes -= {tpl.name_suffix, tpl.points_suffix}
            candidates = suffixes if candidates is None else candidates & suffixes
        for suffix in sorted(candidates or [], key=len):
            values = [as_int(get_path(rows_node, (k,) + suffix)) for k in _row_keys(rows_node)]
            values = [v for v in values if v is not None]
            if values and len(set(values)) == len(values) and max(values) - min(values) == len(values) - 1:
                tpl.position_suffix = suffix
                return

    # Sem campo de posicao: talvez seja a ordem da lista. Linha = ordem na
    # colecao, que para mapa e a ordem das chaves.
    order = {k: i for i, k in enumerate(_row_keys(rows_node))}
    bases = {position - order[key] for position, key in known if key in order}
    if len(bases) == 1:
        tpl.position_index_base = bases.pop()


def _infer_power(tpl: Template, targets: Sequence[Target], hits: Sequence[TargetHits], usable: List[int], name_keys: List[Any]) -> None:
    with_power = [(k, i) for k, i in enumerate(usable) if targets[i].power is not None and hits[i].power]
    if len(with_power) < 2:
        return
    tpls = _single_wildcards([hits[i].power for _, i in with_power])
    for (path, k), pkeys in tpls.items():
        offset = _offset([name_keys[kk] for kk, _ in with_power], pkeys)
        if offset is not None:
            tpl.power_list_path = path[:k]
            tpl.power_suffix = path[k + 1:]
            tpl.power_offset = offset
            return


# ----------------------------------------------------------------- extracao
@dataclass
class Row:
    index: int
    position: Optional[int]
    name: str
    points: int
    power: Optional[int] = None
    record_idx: int = 0
    player_id: Optional[int] = None


UNKNOWN_NAME_PREFIX = "id:"


def extract(tree: Any, tpl: Template, record_idx: int = 0) -> List[Row]:
    rows_node = get_path(tree, tpl.list_path)
    out: List[Row] = []
    for i, key in enumerate(_row_keys(rows_node)):
        name = get_path(tree, tpl.list_path + (key,) + tpl.name_suffix)
        points = as_int(get_path(tree, tpl.points_list_path + (_shift(key, tpl.offset),) + tpl.points_suffix))
        if not isinstance(name, str) or points is None:
            continue
        position = None
        if tpl.position_suffix is not None:
            position = as_int(get_path(tree, tpl.list_path + (key,) + tpl.position_suffix))
        elif tpl.position_index_base is not None:
            position = i + tpl.position_index_base
        power = None
        if tpl.power_list_path is not None:
            power = as_int(get_path(
                tree, tpl.power_list_path + (_shift(key, tpl.power_offset),) + (tpl.power_suffix or ())
            ))
        out.append(Row(i, position, name, points, power, record_idx))
    return out


def extract_everywhere(records: Sequence[dict], base_record: dict, tpl: Template) -> Tuple[List[Row], List[int]]:
    """Aplica o molde a todos os pacotes da mesma rota (ranking paginado).

    Linhas repetidas (mesma posicao, ou mesmo nome sem posicao) ficam com o
    pacote mais recente.
    """
    base_tree = tree_of(base_record)
    route = None
    if tpl.list_path[:1] == ("frames",) and len(tpl.list_path) > 1:
        frame = get_path(base_tree, ("frames", tpl.list_path[1]))
        route = frame.get("route") if isinstance(frame, dict) else None

    merged: Dict[Any, Row] = {}
    used: List[int] = []
    for rec in sorted(records, key=lambda r: r.get("ts", 0)):
        tree = tree_of(rec)
        if tree is None:
            continue
        rows: List[Row] = []
        if route is not None and isinstance(tree, dict) and "frames" in tree:
            for fi, fr in enumerate(tree["frames"]):
                if fr.get("route") == route:
                    rows.extend(extract(tree, tpl.with_frame(fi), rec.get("idx", 0)))
        elif route is None and rec.get("url") == base_record.get("url"):
            rows = extract(tree, tpl, rec.get("idx", 0))
        if rows:
            used.append(rec.get("idx", 0))
        for row in rows:
            key = row.position if row.position is not None else norm(row.name)
            merged[key] = row

    ordered = sorted(merged.values(), key=lambda r: (r.position is None, r.position or 0, -r.points))
    return ordered, used


# ----------------------------------------------------------- juncao por id
# O resultado do torneio (Journal) chega pelo WebSocket como `[[id], pontos]`,
# sem nome. Os nomes vem noutra resposta, a lista de perfis (rota 402), onde
# cada linha tem o mesmo id. O molde abaixo descreve as duas listas e a coluna
# que as liga.
@dataclass
class JoinTemplate:
    points_record_idx: int
    points_list_path: Path
    points_suffix: Path
    points_id_suffix: Path
    names_record_idx: int
    names_route: Optional[int]
    names_list_path: Path
    names_suffix: Path
    names_id_suffix: Path
    position_index_base: Optional[int] = None
    power_suffix: Optional[Path] = None
    style: str = "juncao por id"

    def describe(self) -> List[str]:
        n, m = len(self.points_list_path), len(self.names_list_path)
        lines = [
            f"Estilo: {self.style}",
            f"Ranking (pacote #{self.points_record_idx}):",
            f"   pontos: {format_path(self.points_list_path + (0,) + self.points_suffix, n)}",
            f"   id:     {format_path(self.points_list_path + (0,) + self.points_id_suffix, n)}",
            f"Perfis (pacote #{self.names_record_idx}, rota {self.names_route}):",
            f"   nome:   {format_path(self.names_list_path + (0,) + self.names_suffix, m)}",
            f"   id:     {format_path(self.names_list_path + (0,) + self.names_id_suffix, m)}",
        ]
        if self.power_suffix is not None:
            lines.append(f"   poder:  {format_path(self.names_list_path + (0,) + self.power_suffix, m)}")
        if self.position_index_base is not None:
            lines.append(f"Posicao: ordem da lista {self.position_index_base:+d}")
        else:
            lines.append("Posicao: assumida pela ordem da lista (informe a posicao para confirmar)")
        return lines

    def to_json(self) -> dict:
        data = asdict(self)
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in data.items()}


@dataclass
class _Column:
    list_path: Path
    suffix: Path
    keys: Dict[int, Any]     # indice do alvo -> chave da linha


def _column_values(tree: Any, list_path: Path, suffix: Path) -> List[Any]:
    node = get_path(tree, list_path)
    return [get_path(node, (k,) + suffix) for k in _row_keys(node)]


def _best_column(tree: Any, paths: Dict[int, List[Path]], ranking: bool) -> Optional[_Column]:
    """A lista que contem os valores de mais jogadores ao mesmo tempo.

    Tenta todos os jogadores e, se nao fechar, subconjuntos: um jogador com 0
    pontos, por exemplo, casa com zeros espalhados pelo pacote todo.
    """
    from itertools import combinations

    targets = [i for i, p in paths.items() if p]
    for size in range(len(targets), 1, -1):
        options = []
        for combo in combinations(targets, size):
            for (path, k), keys in _single_wildcards([paths[i] for i in combo]).items():
                list_path, suffix = path[:k], path[k + 1:]
                values = [as_int(v) for v in _column_values(tree, list_path, suffix)]
                numbers = [v for v in values if v is not None]
                descending = all(a >= b for a, b in zip(numbers, numbers[1:]))
                rank = (0 if (descending or not ranking) else 1, -len(values), len(path))
                options.append((rank, _Column(list_path, suffix, dict(zip(combo, keys)))))
        if options:
            options.sort(key=lambda o: o[0])
            return options[0][1]
    return None


def _id_leaves(row: Any, key: Any) -> Dict[Path, int]:
    """Inteiros com cara de id numa linha, mais a propria chave se for numerica."""
    leaves = {p: v for p, v in walk(row) if isinstance(v, int) and not isinstance(v, bool) and v > 1000}
    key_number = as_int(key) if isinstance(key, str) else None
    if key_number is not None and key_number > 1000:
        leaves[("__chave__",)] = key_number
    return leaves


def _row_id(row: Any, key: Any, suffix: Path) -> Optional[int]:
    if suffix == ("__chave__",):
        return as_int(key)
    value = get_path(row, suffix)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def infer_join(
    points_cand: Candidate, names_cand: Candidate, targets: Sequence[Target]
) -> Tuple[Optional[JoinTemplate], str]:
    ptree, ntree = tree_of(points_cand.record), tree_of(names_cand.record)
    pcol = _best_column(ptree, {i: h.points for i, h in enumerate(points_cand.hits)}, ranking=True)
    if pcol is None:
        return None, "Nao achei uma lista com os pontos de 2 ou mais jogadores."
    ncol = _best_column(ntree, {i: h.names for i, h in enumerate(names_cand.hits)}, ranking=False)
    if ncol is None:
        return None, "Nao achei uma lista com os nomes de 2 ou mais jogadores."

    points_node = get_path(ptree, pcol.list_path)

    # Pontuacao repetida (varios jogadores com 0, por exemplo) nao aponta uma
    # linha so. Todas as linhas com aquele valor ficam em jogo e o id decide.
    def rows_with_points(t: int) -> List[Any]:
        return [k for k in _row_keys(points_node)
                if as_int(get_path(points_node, (k,) + pcol.suffix)) == targets[t].points]

    shared = [t for t in sorted(ncol.keys) if rows_with_points(t)]
    if len(shared) < 2:
        return None, "Os jogadores achados na lista de pontos e na de nomes nao sao os mesmos."

    links: Optional[set] = None
    for t in shared:
        nrow = get_path(ntree, ncol.list_path + (ncol.keys[t],))
        nv = _id_leaves(nrow, ncol.keys[t])
        pairs: set = set()
        for pk in rows_with_points(t):
            pv = _id_leaves(get_path(points_node, (pk,)), pk)
            pv.pop(pcol.suffix, None)
            pairs |= {(ps, ns) for ps, a in pv.items() for ns, b in nv.items() if a == b}
        links = pairs if links is None else links & pairs
    if not links:
        return None, "Pontos e nomes estao em listas diferentes e nenhuma coluna de id as liga."

    ps, ns = min(links, key=lambda pair: (len(pair[0]) + len(pair[1]), pair))
    ids = [_row_id(get_path(points_node, (k,)), k, ps) for k in _row_keys(points_node)]
    if len(set(ids)) != len(ids):
        return None, "A coluna que liga as listas tem valores repetidos; nao e um id."

    # Com o id escolhido, cada jogador digitado cai numa linha exata do ranking.
    row_of_id = {pid: k for pid, k in zip(ids, _row_keys(points_node))}
    ranking_key: Dict[int, Any] = {}
    for t in shared:
        nrow = get_path(ntree, ncol.list_path + (ncol.keys[t],))
        pid = _row_id(nrow, ncol.keys[t], ns)
        if pid in row_of_id:
            ranking_key[t] = row_of_id[pid]

    frame = get_path(ntree, ncol.list_path[:2]) if ncol.list_path[:1] == ("frames",) else None
    tpl = JoinTemplate(
        points_record_idx=points_cand.record.get("idx", 0),
        points_list_path=pcol.list_path,
        points_suffix=pcol.suffix,
        points_id_suffix=ps,
        names_record_idx=names_cand.record.get("idx", 0),
        names_route=frame.get("route") if isinstance(frame, dict) else None,
        names_list_path=ncol.list_path,
        names_suffix=ncol.suffix,
        names_id_suffix=ns,
    )

    order = {k: i for i, k in enumerate(_row_keys(points_node))}
    bases = {targets[t].position - order[k] for t, k in ranking_key.items() if targets[t].position is not None}
    if len(bases) == 1:
        tpl.position_index_base = bases.pop()

    with_power = [t for t in shared if targets[t].power is not None]
    if with_power:
        common: Optional[set] = None
        for t in with_power:
            nrow = get_path(ntree, ncol.list_path + (ncol.keys[t],))
            found = {p for p, v in walk(nrow) if as_int(v) == targets[t].power and not isinstance(v, str)}
            common = found if common is None else common & found
        if common:
            tpl.power_suffix = min(common, key=len)
    return tpl, "ok"


def find_join(
    candidates: Sequence[Candidate], chosen: Candidate, targets: Sequence[Target]
) -> Tuple[Optional[JoinTemplate], str]:
    """Casa o pacote escolhido com o melhor parceiro: pontos com nomes, ou o inverso."""
    def count(c: Candidate, attr: str) -> int:
        return sum(1 for h in c.hits if getattr(h, attr))

    if count(chosen, "points") >= 2:
        partners = sorted((c for c in candidates if count(c, "names") >= 2), key=lambda c: -count(c, "names"))
        pairs = [(chosen, p) for p in partners]
    elif count(chosen, "names") >= 2:
        partners = sorted((c for c in candidates if count(c, "points") >= 2), key=lambda c: -count(c, "points"))
        pairs = [(p, chosen) for p in partners]
    else:
        return None, "Este pacote nao tem nomes nem pontos de 2 jogadores."

    last = "Nenhum outro pacote completa este."
    for points_cand, names_cand in pairs[:15]:
        tpl, msg = infer_join(points_cand, names_cand, targets)
        if tpl is not None:
            return tpl, msg
        last = msg
    return None, last


def names_index(records: Sequence[dict], tpl: JoinTemplate) -> Dict[int, Tuple[str, Optional[int]]]:
    """id -> (nome, poder) juntando todas as listas de perfis da gravacao."""
    index: Dict[int, Tuple[str, Optional[int]]] = {}
    sub_path = tpl.names_list_path[2:] if tpl.names_list_path[:1] == ("frames",) else None
    for rec in sorted(records, key=lambda r: r.get("ts", 0)):
        tree = tree_of(rec)
        lists = []
        if tpl.names_route is not None and sub_path is not None and isinstance(tree, dict) and "frames" in tree:
            for fi, fr in enumerate(tree["frames"]):
                if fr.get("route") == tpl.names_route:
                    lists.append(get_path(tree, ("frames", fi) + sub_path))
        elif rec.get("idx") == tpl.names_record_idx:
            lists.append(get_path(tree, tpl.names_list_path))
        for node in lists:
            for key in _row_keys(node):
                row = get_path(node, (key,))
                pid = _row_id(row, key, tpl.names_id_suffix)
                name = get_path(row, tpl.names_suffix)
                if pid is None or not isinstance(name, str):
                    continue
                power = as_int(get_path(row, tpl.power_suffix)) if tpl.power_suffix is not None else None
                index[pid] = (name, power)
    return index


def extract_join(records: Sequence[dict], tpl: JoinTemplate) -> List[Row]:
    points_rec = next((r for r in records if r.get("idx") == tpl.points_record_idx), None)
    if points_rec is None:
        return []
    node = get_path(tree_of(points_rec), tpl.points_list_path)
    index = names_index(records, tpl)
    base = tpl.position_index_base if tpl.position_index_base is not None else 1
    rows: List[Row] = []
    for i, key in enumerate(_row_keys(node)):
        row = get_path(node, (key,))
        points = as_int(get_path(row, tpl.points_suffix))
        pid = _row_id(row, key, tpl.points_id_suffix)
        if points is None or pid is None:
            continue
        name, power = index.get(pid, (f"{UNKNOWN_NAME_PREFIX}{pid}", None))
        rows.append(Row(i, i + base, name, points, power, tpl.points_record_idx, pid))
    return rows


def join_sample(records: Sequence[dict], tpl: JoinTemplate, rows: int = 3) -> List[str]:
    lines: List[str] = []
    for label, idx, path in (
        ("RANKING", tpl.points_record_idx, tpl.points_list_path),
        ("PERFIS", tpl.names_record_idx, tpl.names_list_path),
    ):
        rec = next((r for r in records if r.get("idx") == idx), None)
        node = get_path(tree_of(rec), path) if rec else None
        lines.append(f"{label}: {len(_row_keys(node))} linhas")
        for key in _row_keys(node)[:rows]:
            lines.append("  " + _short(get_path(node, (key,)), 220))
    return lines


def check(rows: Sequence[Row], targets: Sequence[Target]) -> List[Tuple[bool, str]]:
    results: List[Tuple[bool, str]] = []
    results.append((0 < len(rows) <= 100, f"{len(rows)} jogadores extraidos (esperado: 1 a 100)"))

    by_name = {norm(r.name): r for r in rows}
    for t in targets:
        row = by_name.get(norm(t.name)) or next((r for r in rows if norm(t.name) in norm(r.name)), None)
        if row is None:
            results.append((False, f"'{t.name}' nao esta na lista extraida"))
            continue
        ok = row.points == t.points and (t.position is None or row.position == t.position)
        points_text = f"{row.points:,}".replace(",", ".")
        detail = f"pos {row.position}, {points_text} pts"
        results.append((ok, f"'{t.name}': {detail}"))

    positions = [r.position for r in rows if r.position is not None]
    if len(positions) == len(rows) and rows:
        contiguous = sorted(positions) == list(range(min(positions), min(positions) + len(positions)))
        results.append((contiguous and min(positions) == 1, "posicoes unicas e continuas a partir de 1"))
        ordered = sorted(rows, key=lambda r: r.position or 0)
        bad = [r for a, r in zip(ordered, ordered[1:]) if r.points > a.points]
        results.append((not bad, "pontos em ordem decrescente" + (f" (fora de ordem: {', '.join(x.name for x in bad[:5])})" if bad else "")))
    else:
        results.append((False, "posicao nao disponivel em todas as linhas"))

    unknown = [r for r in rows if r.name.startswith(UNKNOWN_NAME_PREFIX)]
    if any(r.player_id is not None for r in rows):
        results.append((
            not unknown,
            "todos os ids com nome" if not unknown else
            f"{len(unknown)} jogador(es) sem nome: o perfil nao veio na gravacao "
            "(abra o perfil dele ou role a lista de membros do clan durante a gravacao)",
        ))

    names = [norm(r.name) for r in rows if not r.name.startswith(UNKNOWN_NAME_PREFIX)]
    dups = sorted({n for n in names if names.count(n) > 1})
    ids = [r.player_id for r in rows if r.player_id is not None]
    if ids:
        # O jogo permite dois jogadores com o mesmo nome no clan; o que nao
        # pode repetir e o id.
        dup_ids = sorted({i for i in ids if ids.count(i) > 1})
        results.append((not dup_ids, "sem ids repetidos" + (f" (repetidos: {dup_ids[:5]})" if dup_ids else "")))
        if dups:
            results.append((True, f"aviso: nomes iguais com ids diferentes: {', '.join(dups[:5])} "
                                  "(sao jogadores distintos; o site deve casar pelo id)"))
    else:
        results.append((not dups, "sem nomes repetidos" + (f" (repetidos: {', '.join(dups[:5])})" if dups else "")))
    return results


def columns_sample(tree: Any, tpl: Template, rows: int = 3) -> List[str]:
    """Todas as colunas das primeiras linhas, para identificar reino, poder, ids."""
    lines: List[str] = []
    node = get_path(tree, tpl.list_path)
    if not isinstance(node, (list, dict)):
        return lines
    if tpl.style != "registro":
        parent = get_path(tree, tpl.list_path[:-1]) if tpl.list_path else None
        lines.append(f"Pai da lista de nomes: {type(parent).__name__}")
        if isinstance(parent, (list, dict)):
            items = parent.items() if isinstance(parent, dict) else enumerate(parent)
            for k, v in items:
                size = len(v) if isinstance(v, (list, dict, str)) else ""
                lines.append(f"  [{k}] {type(v).__name__} {size}  {_short(v)}")
        return lines

    columns: Dict[Path, List[Any]] = {}
    for i, key in enumerate(_row_keys(node)[:rows]):
        if isinstance(node, dict):
            lines.append(f"chave da linha {i}: {_short(key)}")
        for suffix, value in walk(get_path(node, (key,))):
            columns.setdefault(suffix, [None] * rows)[i] = value
    for suffix, values in columns.items():
        label = format_path(suffix).replace("$", "linha") or "linha"
        lines.append(f"{label:<28} " + " | ".join(_short(v) for v in values))
    return lines


def sample_lines(records: Sequence[dict], chosen: Optional[Candidate], template: Any) -> List[str]:
    if template is None or chosen is None:
        return []
    if isinstance(template, JoinTemplate):
        return join_sample(records, template)
    return columns_sample(tree_of(chosen.record), template)


def _short(value: Any, limit: int = 40) -> str:
    if isinstance(value, bytes):
        text = f"bin<{value[:12].hex()}{'...' if len(value) > 12 else ''}>"
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ------------------------------------------------------------------ relatorio
def _jsonable(node: Any) -> Any:
    if isinstance(node, bytes):
        return f"bin<{node.hex()}>"
    if isinstance(node, dict):
        return {str(k): _jsonable(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_jsonable(v) for v in node]
    return node


def export_report(
    out_root: str,
    targets: Sequence[Target],
    records: Sequence[dict],
    candidates: Sequence[Candidate],
    chosen: Optional[Candidate],
    template: Optional[Any],
    rows: Sequence[Row],
    checks: Sequence[Tuple[bool, str]],
    notes: str = "",
) -> str:
    """Grava a pasta do relatorio e um .zip dela. Devolve o caminho do zip.

    Leva so as RESPOSTAS dos pacotes candidatos. Requisicoes nunca foram
    gravadas, entao nao ha sessao do jogo aqui.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(out_root, f"descoberta_{stamp}")
    os.makedirs(folder, exist_ok=True)

    route_counts: Dict[str, int] = {}
    for rec in records:
        for r in dict.fromkeys(rec.get("routes") or ["-"]):
            route_counts[str(r)] = route_counts.get(str(r), 0) + 1

    report = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "ferramenta": "EventUploader/descobrir 0.1",
        "observacoes": notes,
        "digitado": [asdict(t) for t in targets],
        "gravacao": {"respostas": len(records), "rotas": route_counts},
        "candidatos": [
            {
                "idx": c.record.get("idx"),
                "tipo": c.record.get("kind"),
                "url": c.record.get("url"),
                "rotas": c.routes,
                "tamanho": c.record.get("size"),
                "jogadores_completos": c.full,
                "jogadores_parciais": c.partial,
                "caminhos": [
                    {
                        "nome": [format_path(p) for p in h.names[:5]],
                        "pontos": [format_path(p) for p in h.points[:5]],
                        "poder": [format_path(p) for p in h.power[:5]],
                    }
                    for h in c.hits
                ],
            }
            for c in candidates[:15]
        ],
        "escolhido": chosen.record.get("idx") if chosen else None,
        "molde": template.to_json() if template else None,
        "molde_legivel": template.describe() if template else [],
        "colunas": sample_lines(records, chosen, template),
        "verificacoes": [{"ok": ok, "texto": text} for ok, text in checks],
        "ranking": [asdict(r) for r in rows],
    }
    with open(os.path.join(folder, "relatorio.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)

    with open(os.path.join(folder, "ranking.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["posicao", "nome", "pontos", "poder", "id_jogador"])
        for r in rows:
            writer.writerow([r.position, r.name, r.points, r.power, r.player_id])

    chosen_records = [c.record for c in candidates[:5]]
    if isinstance(template, JoinTemplate):
        wanted = {template.points_record_idx, template.names_record_idx}
        chosen_records += [r for r in records if r.get("idx") in wanted]
    seen: set = set()
    with open(os.path.join(folder, "pacotes_candidatos.jsonl"), "w", encoding="utf-8") as fh:
        for rec in chosen_records:
            if rec.get("idx") in seen:
                continue
            seen.add(rec.get("idx"))
            fh.write(json.dumps({
                "idx": rec.get("idx"),
                "tipo": rec.get("kind"),
                "url": rec.get("url"),
                "rotas": rec.get("routes"),
                "b64": rec.get("b64"),
                "decodificado": _jsonable(tree_of(rec)),
            }, ensure_ascii=False) + "\n")

    zip_path = folder + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in os.listdir(folder):
            zf.write(os.path.join(folder, name), name)
    return zip_path
