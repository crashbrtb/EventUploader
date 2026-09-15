"""Mapeador de torneios, com a ferramenta de diagnóstico da Fase 0.

Duas abas:

- Mapear torneios: calibra o Journal e percorre os resultados de torneio para
  descobrir o nome de cada tipo de torneio do jogo, e manda isso ao catálogo do
  site (ver mapper_ui.py).
- Diagnóstico: grava o tráfego enquanto o administrador abre o "Show details"
  de um evento, procura as linhas digitadas e mostra onde o ranking chega. É o
  que se usa se um dia o jogo mudar o formato dos pacotes.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

import capture  # noqa: E402
import finder  # noqa: E402

CDP_PORT = 9222
GAME_URL = "https://totalbattle.com/en/"
REC_DIR = os.path.join(BASE_DIR, "gravacoes")
REPORT_DIR = os.path.join(BASE_DIR, "relatorios")
# Mesmo perfil que o mercs usa: quem ja fez login por la nao precisa refazer.
CHROME_PROFILE = os.path.expanduser(r"~\.total_battle_browser_profile")

INSTRUCTIONS = (
    "1. Com o Total Battle aberto neste Chrome, clique em  Iniciar gravacao.\n"
    "2. No jogo: Journal -> \"Your Clanmates' results in <evento>\" -> Show details.\n"
    "3. Role a lista ate o ultimo jogador (se o ranking vier em partes, isso traz todas).\n"
    "4. Clique em  Parar gravacao.\n"
    "5. Digite 3 linhas que voce ve na tela (o 1o, um do meio e o ultimo) com os PONTOS COMPLETOS.\n"
    "6. Clique em  Procurar,  confira a aba Ranking e clique em  Exportar relatorio.\n"
    "Grave UM evento por vez."
)


def fmt(n: Optional[int]) -> str:
    return "" if n is None else f"{n:,}".replace(",", ".")


def find_chrome() -> Optional[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if path and os.path.exists(path):
            return path
    return None


class DiagnosticFrame(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master)

        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.capture: Optional[capture.CdpCapture] = None
        self.records: List[dict] = []
        self.candidates: List[finder.Candidate] = []
        self.current: Optional[finder.Candidate] = None
        self.template: Optional[finder.Template] = None
        self.rows: List[finder.Row] = []
        self.checks: List[tuple] = []
        self.targets: List[finder.Target] = []

        self._build()
        self.after(300, self._drain)
        self.after(500, self._poll_cdp)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        self.cdp_label = ttk.Label(bar, text="CDP: verificando...", width=26)
        self.cdp_label.pack(side="left")
        ttk.Button(bar, text="Abrir Chrome", command=self._open_chrome).pack(side="left", padx=4)
        self.rec_btn = ttk.Button(bar, text="Iniciar gravacao", command=self._toggle_recording)
        self.rec_btn.pack(side="left", padx=4)
        self.hook_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="Hook na pagina (so se nada for capturado; exige F5 no jogo)", variable=self.hook_var
        ).pack(side="left", padx=8)
        ttk.Button(bar, text="Abrir gravacao...", command=self._load_recording).pack(side="right", padx=4)
        self.count_label = ttk.Label(bar, text="0 respostas")
        self.count_label.pack(side="right", padx=10)

        how = ttk.LabelFrame(self, text="Como fazer")
        how.pack(fill="x", **pad)
        ttk.Label(how, text=INSTRUCTIONS, justify="left").pack(anchor="w", padx=8, pady=4)

        form = ttk.LabelFrame(self, text="Linhas que voce esta vendo na tela")
        form.pack(fill="x", **pad)
        for col, (text, width) in enumerate((("Posicao", 8), ("Nome do jogador", 34), ("Pontos", 20), ("Poder (opcional)", 20))):
            ttk.Label(form, text=text).grid(row=0, column=col, sticky="w", padx=6)
        self.entries: List[List[ttk.Entry]] = []
        for r in range(3):
            line = []
            for col, width in enumerate((8, 34, 20, 20)):
                e = ttk.Entry(form, width=width)
                e.grid(row=r + 1, column=col, sticky="w", padx=6, pady=2)
                line.append(e)
            self.entries.append(line)
        ttk.Label(form, text="Evento / observacoes").grid(row=4, column=0, columnspan=2, sticky="w", padx=6)
        self.notes = ttk.Entry(form, width=70)
        self.notes.grid(row=5, column=0, columnspan=3, sticky="we", padx=6, pady=(0, 6))
        self.search_btn = ttk.Button(form, text="Procurar", command=self._search)
        self.search_btn.grid(row=1, column=4, rowspan=2, padx=16, sticky="ns")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, **pad)

        cand = ttk.Frame(self.tabs)
        self.tabs.add(cand, text="Pacotes candidatos")
        cols = ("idx", "tipo", "rotas", "nome+pontos", "nomes", "pontos", "tamanho", "url")
        self.cand_tree = ttk.Treeview(cand, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (60, 70, 120, 90, 60, 60, 90, 460)):
            self.cand_tree.heading(c, text=c)
            self.cand_tree.column(c, width=w, anchor="w")
        self.cand_tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(cand, orient="vertical", command=self.cand_tree.yview)
        sb.pack(side="right", fill="y")
        self.cand_tree.configure(yscrollcommand=sb.set)
        self.cand_tree.bind("<<TreeviewSelect>>", self._on_candidate)

        rank = ttk.Frame(self.tabs)
        self.tabs.add(rank, text="Ranking extraido")
        self.check_text = tk.Text(rank, height=8, wrap="word")
        self.check_text.pack(fill="x")
        rcols = ("posicao", "nome", "pontos", "poder", "id jogador", "pacote")
        self.rank_tree = ttk.Treeview(rank, columns=rcols, show="headings")
        for c, w in zip(rcols, (70, 300, 170, 170, 130, 70)):
            self.rank_tree.heading(c, text=c)
            self.rank_tree.column(c, width=w, anchor="e" if c in ("pontos", "poder") else "w")
        self.rank_tree.pack(fill="both", expand=True, side="left")
        sb2 = ttk.Scrollbar(rank, orient="vertical", command=self.rank_tree.yview)
        sb2.pack(side="right", fill="y")
        self.rank_tree.configure(yscrollcommand=sb2.set)

        struct = ttk.Frame(self.tabs)
        self.tabs.add(struct, text="Estrutura do pacote")
        self.struct_text = tk.Text(struct, wrap="none", font=("Consolas", 9))
        self.struct_text.pack(fill="both", expand=True)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="Exportar relatorio", command=self._export).pack(side="left")
        ttk.Button(bottom, text="Abrir pasta de relatorios", command=lambda: self._open_folder(REPORT_DIR)).pack(side="left", padx=6)
        self.status = ttk.Label(bottom, text="Pronto.")
        self.status.pack(side="left", padx=12)

    # -------------------------------------------------------------- utilidades
    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _open_folder(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        webbrowser.open(path)

    def _poll_cdp(self) -> None:
        def probe():
            probe_cap = capture.CdpCapture(port=CDP_PORT)
            ok = probe_cap.cdp_available()
            tab = probe_cap.find_game_tab() if ok else None
            self.inbox.put({"__cdp__": (ok, tab is not None)})
        threading.Thread(target=probe, daemon=True).start()
        self.after(3000, self._poll_cdp)

    def _drain(self) -> None:
        changed = False
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if "__cdp__" in item:
                ok, game = item["__cdp__"]
                text = "CDP: jogo encontrado" if game else ("CDP: sem aba do jogo" if ok else "CDP: Chrome sem depuracao")
                self.cdp_label.configure(text=text, foreground="#2e7d32" if game else "#c62828")
            elif "__status__" in item:
                self._set_status(item["__status__"])
            elif "__record__" in item:
                self.records.append(item["__record__"])
                changed = True
            elif "__search__" in item:
                self._show_candidates(item["__search__"])
        if changed:
            self._update_count()
        self.after(300, self._drain)

    def _update_count(self) -> None:
        stats = self.capture.stats if self.capture else {}
        extra = f"  (req {stats.get('REQ', 0)}, ws {stats.get('WS_IN', 0)}, hook {stats.get('HOOK', 0)})" if stats else ""
        self.count_label.configure(text=f"{len(self.records)} respostas{extra}")

    # ----------------------------------------------------------------- chrome
    def _open_chrome(self) -> None:
        if capture.CdpCapture(port=CDP_PORT).cdp_available():
            messagebox.showinfo("Chrome", "O Chrome ja esta aberto com depuracao na porta 9222.")
            return
        exe = find_chrome()
        if not exe:
            messagebox.showerror("Chrome", "Chrome nao encontrado. Abra manualmente com --remote-debugging-port=9222.")
            return
        os.makedirs(CHROME_PROFILE, exist_ok=True)
        subprocess.Popen([
            exe, f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*",
            f"--user-data-dir={CHROME_PROFILE}", "--no-first-run", "--no-default-browser-check",
            "--start-maximized", GAME_URL,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._set_status("Chrome aberto. Faca login no jogo se for preciso.")

    # ---------------------------------------------------------------- gravacao
    def _toggle_recording(self) -> None:
        if self.capture and self.capture.is_running:
            path = self.capture.stop()
            self.rec_btn.configure(text="Iniciar gravacao")
            self._update_count()
            self._set_status(f"Gravacao parada: {len(self.records)} respostas em {os.path.basename(path or '')}.")
            return

        if self.records and not messagebox.askyesno("Nova gravacao", "Descartar a gravacao atual e comecar outra?"):
            return
        self.records = []
        finder.clear_cache()
        self.capture = capture.CdpCapture(
            port=CDP_PORT,
            out_dir=REC_DIR,
            on_status=lambda m: self.inbox.put({"__status__": m}),
            on_record=lambda r: self.inbox.put({"__record__": r}),
            use_page_hook=self.hook_var.get(),
        )
        if self.capture.start():
            self.rec_btn.configure(text="Parar gravacao")
            self._update_count()

    def _load_recording(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=REC_DIR, filetypes=[("Gravacoes", "*.jsonl"), ("Todos", "*.*")]
        )
        if not path:
            return
        finder.clear_cache()
        self.records = capture.load_recording(path)
        self.capture = None
        self._update_count()
        self._set_status(f"Gravacao carregada: {os.path.basename(path)}")

    # ------------------------------------------------------------------- busca
    def _read_targets(self) -> Optional[List[finder.Target]]:
        targets = []
        for pos_e, name_e, pts_e, pow_e in self.entries:
            name = name_e.get().strip()
            points = finder.parse_int(pts_e.get())
            if not name and points is None:
                continue
            if not name or points is None:
                messagebox.showwarning("Linha incompleta", "Cada linha precisa de nome e pontos.")
                return None
            targets.append(finder.Target(
                name=name, points=points,
                position=finder.parse_int(pos_e.get()), power=finder.parse_int(pow_e.get()),
            ))
        if len(targets) < 2:
            messagebox.showwarning("Poucas linhas", "Digite pelo menos 2 jogadores (3 e melhor).")
            return None
        return targets

    def _search(self) -> None:
        if self.capture and self.capture.is_running:
            messagebox.showinfo("Gravando", "Pare a gravacao antes de procurar.")
            return
        if not self.records:
            messagebox.showinfo("Sem dados", "Grave (ou abra) uma gravacao primeiro.")
            return
        targets = self._read_targets()
        if not targets:
            return
        self.targets = targets
        self.search_btn.configure(state="disabled")
        self._set_status(f"Procurando em {len(self.records)} respostas...")
        records = list(self.records)

        def work():
            try:
                result = finder.search(records, targets)
            except Exception as exc:  # erro inesperado vira mensagem, nao trava a janela
                self.inbox.put({"__status__": f"Erro na busca: {exc}"})
                result = []
            self.inbox.put({"__search__": result})
        threading.Thread(target=work, daemon=True).start()

    def _show_candidates(self, candidates: List[finder.Candidate]) -> None:
        self.search_btn.configure(state="normal")
        self.candidates = candidates
        self.cand_tree.delete(*self.cand_tree.get_children())
        total = len(self.targets)
        for i, c in enumerate(candidates[:200]):
            rec = c.record
            self.cand_tree.insert("", "end", iid=str(i), values=(
                rec.get("idx"), rec.get("kind"), c.routes, f"{c.full}/{total}",
                f"{sum(1 for h in c.hits if h.names)}/{total}",
                f"{sum(1 for h in c.hits if h.points)}/{total}",
                fmt(rec.get("size")), rec.get("url"),
            ))
        self.tabs.select(0)
        if not candidates:
            self._set_status("Nenhum pacote contem esses nomes ou pontos. Confira o que foi digitado.")
            self._write_checks([(False, "Nada encontrado. Veja o README, secao 'Se nada for encontrado'.")])
            return
        start = finder.best_names_and_points(candidates)
        self._set_status(f"{len(candidates)} pacote(s) candidatos. Analisando o melhor...")
        self.cand_tree.selection_set(str(candidates.index(start)))

    def _on_candidate(self, _event=None) -> None:
        sel = self.cand_tree.selection()
        if not sel:
            return
        self._analyze(self.candidates[int(sel[0])])

    def _analyze(self, cand: finder.Candidate) -> None:
        self.current = cand
        tree = finder.tree_of(cand.record)
        tpl, msg = finder.infer_template(tree, self.targets, cand.hits)
        self.template = tpl

        lines = [f"Pacote #{cand.record.get('idx')}  rotas {cand.routes}  {cand.record.get('url')}", ""]
        for t, h in zip(self.targets, cand.hits):
            lines.append(f"'{t.name}':")
            lines.append("   nome em   " + (", ".join(finder.format_path(p) for p in h.names[:4]) or "-"))
            lines.append("   pontos em " + (", ".join(finder.format_path(p) for p in h.points[:4]) or "-"))
            if t.power is not None:
                lines.append("   poder em  " + (", ".join(finder.format_path(p) for p in h.power[:4]) or "-"))
        lines.append("")

        if tpl is None:
            join, join_msg = finder.find_join(self.candidates, cand, self.targets)
            if join is None:
                self.rows, self.checks = [], [(False, msg), (False, "Juncao por id: " + join_msg)]
                lines += [msg, "Juncao por id: " + join_msg]
            else:
                self.template = tpl = join
                self.current = next(
                    (c for c in self.candidates if c.record.get("idx") == join.points_record_idx), cand
                )
                self.rows = finder.extract_join(self.records, join)
                self.checks = finder.check(self.rows, self.targets)
                named = sum(1 for r in self.rows if not r.name.startswith(finder.UNKNOWN_NAME_PREFIX))
                lines.append(
                    f"Ranking por id no pacote #{join.points_record_idx}; nomes de {named} de "
                    f"{len(self.rows)} jogadores vieram das listas de perfis da gravacao."
                )
                lines += ["", "MOLDE"] + ["  " + x for x in join.describe()]
                lines += ["", "AMOSTRA"] + ["  " + x for x in finder.join_sample(self.records, join)]
        else:
            single = finder.extract(tree, tpl, cand.record.get("idx", 0))
            merged, used = finder.extract_everywhere(self.records, cand.record, tpl)
            if len(used) > 1 and len(merged) > len(single):
                self.rows = merged
                lines.append(f"Ranking montado juntando {len(used)} pacotes da mesma rota: {used}")
            else:
                self.rows = single
                lines.append("Ranking completo neste unico pacote.")
            self.checks = finder.check(self.rows, self.targets)
            lines += ["", "MOLDE"] + ["  " + x for x in tpl.describe()]
            lines += ["", "COLUNAS DAS 3 PRIMEIRAS LINHAS (para achar reino, poder, ids)"]
            lines += ["  " + x for x in finder.columns_sample(tree, tpl)]

        self.struct_text.delete("1.0", "end")
        self.struct_text.insert("end", "\n".join(lines))
        self._write_checks(self.checks)

        self.rank_tree.delete(*self.rank_tree.get_children())
        for row in self.rows:
            self.rank_tree.insert("", "end", values=(
                "" if row.position is None else row.position, row.name, fmt(row.points), fmt(row.power),
                "" if row.player_id is None else row.player_id, row.record_idx,
            ))
        ok = bool(self.checks) and all(c[0] for c in self.checks)
        self._set_status(
            f"{len(self.rows)} jogadores extraidos. " + ("Tudo confere - exporte o relatorio." if ok else "Ha verificacoes falhando - veja a aba Ranking.")
        )
        self.tabs.select(1 if tpl else 2)

    def _write_checks(self, checks) -> None:
        self.check_text.delete("1.0", "end")
        for ok, text in checks:
            self.check_text.insert("end", ("[OK]   " if ok else "[FALHA] ") + text + "\n")

    # --------------------------------------------------------------- relatorio
    def _export(self) -> None:
        if not self.candidates:
            messagebox.showinfo("Nada para exportar", "Faca uma busca primeiro.")
            return
        path = finder.export_report(
            REPORT_DIR, self.targets, self.records, self.candidates, self.current,
            self.template, self.rows, self.checks, self.notes.get().strip(),
        )
        messagebox.showinfo(
            "Relatorio exportado",
            f"Gerado:\n{path}\n\nEle contem so respostas do servidor (ranking e dados publicos do jogo), "
            "nunca o corpo das requisicoes com a sua sessao.",
        )
        self._open_folder(REPORT_DIR)

    def shutdown(self) -> None:
        if self.capture and self.capture.is_running:
            self.capture.stop()


class MainWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EventUploader - Mapeador de torneios")
        self.geometry("1240x860")
        self.minsize(960, 680)

        import mapper_ui  # noqa: E402 - needs the working directory set above

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.mapper = mapper_ui.MapperFrame(tabs)
        tabs.add(self.mapper, text="Mapear torneios")
        self.diagnostic = DiagnosticFrame(tabs)
        tabs.add(self.diagnostic, text="Diagnóstico")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.mapper.shutdown()
        self.diagnostic.shutdown()
        self.destroy()


if __name__ == "__main__":
    MainWindow().mainloop()
