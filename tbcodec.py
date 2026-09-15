"""Decodificador do protocolo do Total Battle.

Copiado de `C:\\Total Battle Tools\\mercs\\core\\tb_codec.py`, onde foi validado
contra os logs do jogo. Copiado (e nao importado) porque este app precisa rodar
sozinho na maquina de outros administradores, sem o projeto mercs ao lado.

O jogo NAO usa MessagePack padrao: tamanhos e inteiros multi-byte vem em
little-endian. A lib `msgpack` comum embaralha todos os inteiros - e pontuacao de
torneio e justamente um inteiro grande.

Enquadramento de cada resposta:

    [u32 LE total_len][u32 LE inner_len][msgpack LE ...][cauda de eventos]

O primeiro objeto e sempre `[route_id, seq]`. A rota 2 e um lote: depois do
cabecalho vem a quantidade de sub-frames e os sub-frames concatenados. Depois de
`inner_len` pode vir uma cauda de eventos `[u32 len][msgpack]`.
"""
from __future__ import annotations

import gzip
import json
import struct
import zlib
from typing import Any, Iterator, List, NamedTuple, Optional

try:
    import zstandard
except ImportError:  # opcional: so e usado se o servidor mandar zstd
    zstandard = None

ROUTE_BATCH = 2


class TBDecodeError(ValueError):
    pass


class LEUnpacker:
    """MessagePack com campos multi-byte em little-endian."""

    # Pacotes reais do jogo nao passam de algumas dezenas de niveis. Bytes que
    # nao sao do jogo (assets, JSON binario) podem parecer mapas dentro de mapas
    # sem fim; sem este teto a leitura estoura a pilha do Python.
    MAX_DEPTH = 120

    def __init__(self, buf: bytes, pos: int = 0):
        self.b = buf
        self.p = pos
        self.depth = 0

    def _u(self, fmt: str, n: int):
        try:
            v = struct.unpack_from(fmt, self.b, self.p)[0]
        except struct.error as exc:
            raise TBDecodeError(f"leitura fora do buffer em {self.p}") from exc
        self.p += n
        return v

    def read(self) -> Any:
        try:
            b = self.b[self.p]
        except IndexError:
            raise TBDecodeError(f"fim inesperado do buffer em {self.p}")
        self.p += 1

        if b <= 0x7F:
            return b
        if b >= 0xE0:
            return b - 256
        if 0x80 <= b <= 0x8F:
            return self._map(b & 0x0F)
        if 0x90 <= b <= 0x9F:
            return self._array(b & 0x0F)
        if 0xA0 <= b <= 0xBF:
            return self._str(b & 0x1F)

        simple = {
            0xC0: None, 0xC2: False, 0xC3: True,
        }
        if b in simple:
            return simple[b]
        if b == 0xC4:
            return self._bin(self._u("<B", 1))
        if b == 0xC5:
            return self._bin(self._u("<H", 2))
        if b == 0xC6:
            return self._bin(self._u("<I", 4))
        if b == 0xCA:
            return self._u("<f", 4)
        if b == 0xCB:
            return self._u("<d", 8)
        if b == 0xCC:
            return self._u("<B", 1)
        if b == 0xCD:
            return self._u("<H", 2)
        if b == 0xCE:
            return self._u("<I", 4)
        if b == 0xCF:
            return self._u("<Q", 8)
        if b == 0xD0:
            return self._u("<b", 1)
        if b == 0xD1:
            return self._u("<h", 2)
        if b == 0xD2:
            return self._u("<i", 4)
        if b == 0xD3:
            return self._u("<q", 8)
        if b == 0xD9:
            return self._str(self._u("<B", 1))
        if b == 0xDA:
            return self._str(self._u("<H", 2))
        if b == 0xDB:
            return self._str(self._u("<I", 4))
        if b == 0xDC:
            return self._array(self._u("<H", 2))
        if b == 0xDD:
            return self._array(self._u("<I", 4))
        if b == 0xDE:
            return self._map(self._u("<H", 2))
        if b == 0xDF:
            return self._map(self._u("<I", 4))
        if 0xD4 <= b <= 0xD8:
            return self._ext(1 << (b - 0xD4))
        if b == 0xC7:
            return self._ext(self._u("<B", 1))
        if b == 0xC8:
            return self._ext(self._u("<H", 2))
        if b == 0xC9:
            return self._ext(self._u("<I", 4))

        raise TBDecodeError(f"byte de formato desconhecido 0x{b:02x} em {self.p - 1}")

    # Cada elemento ocupa pelo menos um byte: um tamanho maior que o que resta do
    # buffer so pode ser lixo, e recusar cedo evita alocar listas gigantes ao
    # tentar decodificar algo que nem e pacote do jogo.
    def _array(self, n: int) -> List[Any]:
        if n > len(self.b) - self.p:
            raise TBDecodeError(f"array impossivel ({n}) em {self.p}")
        self._enter()
        try:
            return [self.read() for _ in range(n)]
        finally:
            self.depth -= 1

    def _map(self, n: int) -> dict:
        if 2 * n > len(self.b) - self.p:
            raise TBDecodeError(f"map impossivel ({n}) em {self.p}")
        self._enter()
        try:
            return {str(self.read()): self.read() for _ in range(n)}
        finally:
            self.depth -= 1

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > self.MAX_DEPTH:
            raise TBDecodeError(f"aninhamento acima de {self.MAX_DEPTH} em {self.p}")

    def _str(self, n: int) -> str:
        s = self.b[self.p:self.p + n]
        if len(s) != n:
            raise TBDecodeError(f"string truncada em {self.p}")
        self.p += n
        return s.decode("utf-8", "replace")

    def _bin(self, n: int) -> bytes:
        s = self.b[self.p:self.p + n]
        if len(s) != n:
            raise TBDecodeError(f"bin truncado em {self.p}")
        self.p += n
        return s

    def _ext(self, n: int) -> dict:
        code = self.b[self.p] if self.p < len(self.b) else 0
        payload = self.b[self.p + 1:self.p + 1 + n]
        self.p += 1 + n
        return {"__ext__": code, "data": payload}


def unpack(buf: bytes, pos: int = 0) -> Any:
    return LEUnpacker(buf, pos).read()


def pack(obj: Any) -> bytes:
    """Codifica no dialeto do jogo. Usado nos testes para montar pacotes falsos."""
    if obj is None:
        return b"\xc0"
    if isinstance(obj, bool):
        return b"\xc3" if obj else b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xFF])
        if 0 <= obj <= 0xFF:
            return b"\xcc" + struct.pack("<B", obj)
        if 0 <= obj <= 0xFFFF:
            return b"\xcd" + struct.pack("<H", obj)
        if 0 <= obj <= 0xFFFFFFFF:
            return b"\xce" + struct.pack("<I", obj)
        if 0 <= obj:
            return b"\xcf" + struct.pack("<Q", obj)
        if -0x80 <= obj:
            return b"\xd0" + struct.pack("<b", obj)
        if -0x8000 <= obj:
            return b"\xd1" + struct.pack("<h", obj)
        if -0x80000000 <= obj:
            return b"\xd2" + struct.pack("<i", obj)
        return b"\xd3" + struct.pack("<q", obj)
    if isinstance(obj, float):
        return b"\xcb" + struct.pack("<d", obj)
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        if len(raw) < 32:
            return bytes([0xA0 | len(raw)]) + raw
        if len(raw) <= 0xFF:
            return b"\xd9" + struct.pack("<B", len(raw)) + raw
        if len(raw) <= 0xFFFF:
            return b"\xda" + struct.pack("<H", len(raw)) + raw
        return b"\xdb" + struct.pack("<I", len(raw)) + raw
    if isinstance(obj, (bytes, bytearray)):
        raw = bytes(obj)
        if len(raw) <= 0xFF:
            return b"\xc4" + struct.pack("<B", len(raw)) + raw
        return b"\xc6" + struct.pack("<I", len(raw)) + raw
    if isinstance(obj, (list, tuple)):
        items = b"".join(pack(x) for x in obj)
        if len(obj) < 16:
            return bytes([0x90 | len(obj)]) + items
        if len(obj) <= 0xFFFF:
            return b"\xdc" + struct.pack("<H", len(obj)) + items
        return b"\xdd" + struct.pack("<I", len(obj)) + items
    if isinstance(obj, dict):
        items = b"".join(pack(k) + pack(v) for k, v in obj.items())
        if len(obj) < 16:
            return bytes([0x80 | len(obj)]) + items
        if len(obj) <= 0xFFFF:
            return b"\xde" + struct.pack("<H", len(obj)) + items
        return b"\xdf" + struct.pack("<I", len(obj)) + items
    raise TypeError(f"tipo nao suportado pelo codec: {type(obj).__name__}")


def build_frame(route: int, seq: int, *objects: Any, tail_events: tuple = ()) -> bytes:
    """Monta um frame completo. So para testes."""
    body = pack([route, seq]) + b"".join(pack(o) for o in objects)
    tail = b"".join(struct.pack("<I", len(e)) + e for e in (pack(ev) for ev in tail_events))
    inner = 8 + len(body)
    return struct.pack("<II", inner + len(tail), inner) + body + tail


class Frame(NamedTuple):
    route: Optional[int]
    seq: Optional[int]
    objects: List[Any]
    offset: int
    length: int
    error: Optional[str]
    tail: bytes = b""


def iter_frames(buf: bytes, pos: int = 0, end: Optional[int] = None, depth: int = 0) -> Iterator[Frame]:
    """Percorre os frames de um payload, desembrulhando lotes (rota 2)."""
    if end is None:
        end = len(buf)

    while pos + 8 <= end:
        total, inner = struct.unpack_from("<II", buf, pos)
        if total < 8 or pos + total > end:
            total = end - pos

        frame_end = pos + total
        content_end = pos + inner if 8 <= inner <= total else frame_end
        u = LEUnpacker(buf, pos + 8)
        header: Any = None
        error: Optional[str] = None
        try:
            header = u.read()
        except TBDecodeError as exc:
            error = str(exc)

        route = seq = None
        if isinstance(header, list) and len(header) >= 2 and isinstance(header[0], int):
            route, seq = header[0], header[1]

        if route == ROUTE_BATCH and error is None and depth < 4:
            try:
                u.read()
            except TBDecodeError as exc:
                yield Frame(route, seq, [], pos, total, str(exc))
            else:
                yield from iter_frames(buf, u.p, frame_end, depth + 1)
        else:
            objects: List[Any] = []
            if error is None:
                try:
                    while u.p < content_end:
                        objects.append(u.read())
                except TBDecodeError as exc:
                    error = str(exc)
            yield Frame(route, seq, objects, pos, total, error, buf[content_end:frame_end])

        pos = frame_end


def decode_tail(tail: bytes) -> List[Any]:
    """Eventos anexados depois do corpo: `[u32 len][msgpack]` repetido."""
    events: List[Any] = []
    pos = 0
    while pos + 4 <= len(tail):
        (n,) = struct.unpack_from("<I", tail, pos)
        chunk = tail[pos + 4:pos + 4 + n]
        if n == 0 or len(chunk) != n:
            break
        try:
            events.append(unpack(chunk))
        except TBDecodeError:
            break
        pos += 4 + n
    return events


def _decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    if zstandard is not None and data[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return zstandard.ZstdDecompressor().decompress(data)
        except zstandard.ZstdError:
            return data
    if len(data) > 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        try:
            return zlib.decompress(data)
        except zlib.error:
            return data
    return data


def unpack_stream(data: bytes) -> Optional[List[Any]]:
    """Objetos msgpack LE concatenados, sem enquadramento, ocupando o buffer inteiro.

    E o formato do WebSocket de notificacoes (o Journal chega por ali) e dos
    blobs binarios aninhados. So conta se consumir tudo sem erro: assim bytes
    quaisquer nao viram uma arvore de lixo.
    """
    if not data:
        return None
    u = LEUnpacker(data)
    objects: List[Any] = []
    try:
        while u.p < len(data):
            objects.append(u.read())
    except (TBDecodeError, RecursionError):
        return None
    if not objects or not isinstance(objects[0], (list, dict)):
        return None
    return objects


MAX_BLOB_DEPTH = 6


def expand_blobs(node: Any, depth: int = 0) -> Any:
    """Troca blobs binarios que sao msgpack por `{"blob": [objetos]}`.

    O jogo embrulha mensagens inteiras (o resultado do torneio, por exemplo)
    dentro de um campo binario de outra mensagem.
    """
    if isinstance(node, bytes):
        if depth < MAX_BLOB_DEPTH and len(node) >= 4:
            inner = unpack_stream(node)
            if inner is not None:
                return {"blob": [expand_blobs(x, depth + 1) for x in inner]}
        return node
    if isinstance(node, list):
        return [expand_blobs(x, depth) for x in node]
    if isinstance(node, dict):
        return {k: expand_blobs(v, depth) for k, v in node.items()}
    return node


def decode_payload(data: bytes) -> Optional[Any]:
    """Transforma o corpo de uma resposta ou frame numa arvore navegavel, ou None.

    - frames HTTP do jogo: `{"frames": [{"route", "seq", "objects", "events"}]}`
    - WebSocket / msgpack sem enquadramento: `{"stream": [objetos]}`
    - texto com etiqueta (`PONG{...}`): `{"tag": "PONG", "json": {...}}`
    - JSON: o proprio objeto
    Blobs binarios aninhados que sao msgpack sao abertos. Imagem, script e
    asset viram None.
    """
    if not data:
        return None
    data = _decompress(data)

    stripped = data.lstrip()
    if stripped[:1] in (b"{", b"["):
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            pass

    if len(data) > 5 and data[:4].isalpha() and data[:4].isupper() and data[4:5] == b"{":
        try:
            return {"tag": data[:4].decode("ascii"), "json": json.loads(data[4:].decode("utf-8"))}
        except (UnicodeDecodeError, ValueError):
            pass

    # Resposta HTTP do jogo comeca pelo tamanho total do frame. Se o cabecalho nao
    # fecha com o buffer, e mais provavel ser msgpack puro (WebSocket).
    (declared,) = struct.unpack_from("<I", data) if len(data) >= 4 else (0,)
    if not 8 <= declared <= len(data):
        stream = unpack_stream(data)
        if stream is not None:
            return expand_blobs({"stream": stream})

    frames = []
    try:
        for frame in iter_frames(data):
            if frame.route is None or (frame.error and not frame.objects):
                continue
            frames.append({
                "route": frame.route,
                "seq": frame.seq,
                "objects": frame.objects,
                "events": decode_tail(frame.tail) if frame.tail else [],
                "error": frame.error,
            })
    except RecursionError:
        frames = []
    if frames:
        return expand_blobs({"frames": frames})

    stream = unpack_stream(data)
    if stream is not None:
        return expand_blobs({"stream": stream})
    return None
