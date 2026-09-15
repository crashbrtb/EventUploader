# EventUploader

Envia os resultados dos **torneios do jogo** (Total Battle) para o site do clã
(chestcounter). O app lê o ranking que o próprio jogo recebe, junta os nomes
dos jogadores e manda tudo para o site, que **cadastra o torneio sozinho** e
deixa o rateio dos prêmios pronto para revisar e publicar.

O envio diário não lê a tela nem clica no jogo: só escuta o tráfego do Chrome.
O **mapeador de torneios** (`MapearTorneios.bat`) é a exceção: ele navega no
Journal sozinho para descobrir o nome de cada tipo de torneio. Nenhuma das duas
ferramentas acessa o banco do site; tudo passa pela API com um token.

## Instalar

1. Python 3.10+ (com tkinter, que já vem no instalador oficial).
2. Para o mapeador: [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
   (o pacote de inglês basta).
3. Duplo clique em `install.bat`.

## Configurar (uma vez)

1. No site, como administrador: **Admin › Events › API Tokens › Criar token**.
   Copie o token (ele só aparece uma vez).
2. Abra o app com `EventUploader.bat`, preencha **Endereço** (ex.:
   `https://kokmain.counter.li`) e **Token de API**, e clique em **Salvar e
   testar**. Deve aparecer "Conectado como ...".

O token fica no Cofre de Credenciais do Windows, não em arquivo.

## Uso diário

1. Com o Total Battle aberto no Chrome com depuração (botão **Abrir Chrome**,
   ou o Chrome que o mercs abre), clique em **Conectar ao jogo**.
2. No jogo, abra o **Journal** e clique em **Show details** no resultado do
   torneio.
3. O torneio aparece em **Torneios encontrados** com o ranking completo e as
   verificações (quantidade, ordem dos pontos, nomes, data, tipo).
4. Confira o **Nome do torneio** e clique em **Enviar para o site**.
5. O app mostra o que o site fez e oferece abrir a **revisão**, onde você
   confere quem recebe, ajusta e publica.

### O que o site faz ao receber

- **Torneio novo:** cria o evento do jogo com a data real de término. O
  **início** é o término menos a **duração** cadastrada no catálogo de torneios
  (um dia quando não há duração); dá para ajustar na revisão.
- **Tipo já conhecido:** o nome vem do catálogo e os **prêmios** do último
  torneio do mesmo tipo. Nos torneios diários, é só revisar e publicar.
- **Membros do clã:** o ranking traz o clã inteiro (quem fez 0 pontos também),
  então ele é a fonte da verdade da tabela de membros. Quem está no ranking fica
  **ativo**, com poder e id do jogo atualizados (membro novo é criado, nome
  trocado é seguido pelo id); quem não está fica **inativo**. Um torneio mais
  antigo que o último já aplicado não mexe nos membros.
- **Mesmo resultado enviado de novo:** cai no mesmo evento; se o ranking for
  igual, nada muda.
- **Resultado já publicado:** o site recusa. Despublique na revisão para
  reenviar.

O jogo **não envia o nome do torneio**, só o tipo (ex.: `1024` em `1024:1`; o
número depois dos dois pontos muda a cada edição). Os nomes ficam no **catálogo
de torneios** do site, preenchido pelo mapeador. Se um tipo ainda não tiver
nome, digite no app (ou corrija no site depois) e cadastre os prêmios na
revisão. A partir daí, é automático.

### Se algo faltar

| Aviso | O que fazer |
|---|---|
| Faltam nomes | Feche e abra o "Show details" de novo: o jogo reenvia os perfis. |
| Data de término desconhecida | Abra a **lista** do Journal com o app conectado, ou digite a data (`12/09/2026 17:00`, UTC). |
| "Abra o Show details deste torneio" | O app viu o torneio na lista, mas o ranking ainda não chegou. |
| 401 / token inválido | Crie outro token no site e salve no app. |
| 409 | O resultado já foi publicado no site. |

## Mapear torneios (catálogo)

`MapearTorneios.bat`, aba **Mapear torneios**. Faça quando aparecer um torneio
novo no jogo; não é uma tarefa diária.

1. Preencha o site e o token (os mesmos do EventUploader) e **Salvar e testar**.
   Os nomes que o catálogo já tem são carregados para não abrir de novo.
2. **Calibrar Journal...** (uma vez, ou quando o layout do jogo mudar). O
   assistente mostra a tela do jogo; em cada passo clique no ponto ou marque a
   área pedida:
   botão do Journal e aba de eventos (opcionais), título do 1º e do 2º card (a
   distância entre eles localiza os demais), ícone do card (vira a imagem do
   torneio no site), número da página, seta de próxima página, abrir o 1º card,
   botão **Show details** (procurado pela imagem, porque muda de lugar), fechar
   o ranking e fechar o card. Informe quantos cards cabem numa página e use
   **Testar leitura dos cards** para conferir o OCR dos títulos.
3. **Iniciar mapeamento.** Não mexa no jogo enquanto roda. Para cada card cujo
   título começa com `Your Clanmates' results in <evento>` o mapeador abre
   o card, aperta **Show details**, espera o ranking chegar pela rede para ler o
   **id do tipo** e volta para a lista; depois passa de página até a última (ou
   o limite de páginas). Cards que não são torneio e nomes já conhecidos são
   pulados.
4. Confira a tabela e clique em **Enviar ao catálogo do site**. Nome editado à
   mão no site nunca é sobrescrito; a imagem só é enviada se o torneio ainda
   não tiver uma.
5. Opcional: **Enviar também os rankings abertos** cadastra no site os torneios
   que o mapeador abriu, do mais antigo para o mais recente.

No site, **Admin › Tournament Catalogue** permite editar nome, **imagem** e
**duração em dias** de cada torneio.

| Situação no mapeamento | O que significa |
|---|---|
| sem resposta do jogo | O jogo já tinha o ranking em cache e não o reenviou. Rode de novo depois de recarregar o jogo (F5). |
| "não voltou para a lista" | O fechar do card/ranking não funcionou: refaça esses passos na calibração. |
| Leituras conflitantes | O OCR leu nomes diferentes para o mesmo tipo (ou o mesmo nome para tipos diferentes). Corrija no catálogo do site. |

## Como funciona (para manutenção)

| Arquivo | Papel |
|---|---|
| `capture.py` | Conecta no Chrome pela porta 9222 (CDP) e recebe as respostas e os frames de WebSocket do jogo. Nunca guarda o corpo das requisições (a sessão do jogo viaja nele). |
| `tbcodec.py` | Decodifica o protocolo do jogo: MessagePack little-endian, frames HTTP, WebSocket e mensagens embrulhadas em blobs. |
| `journal.py` | Monta os torneios: entrada do Journal (tipo e término), detalhe (ranking por id) e perfis da rota 402 (nomes). |
| `api.py` | Cliente da API do site (`/api/v1`). Manda o token em `Authorization` e em `X-Api-Token`. |
| `app.py` | A janela do envio diário. |
| `browser.py` | Captura de tela e cliques no jogo pelo CDP, e busca de imagem (OpenCV). |
| `calibration.py` | Passos da calibração, gravados em `%APPDATA%\EventUploader\calibracao_journal.json` e reescalados com o tamanho da janela. |
| `wizard.py` | O assistente de calibração. |
| `titles.py` | OCR (Tesseract) dos títulos dos cards e extração do nome do torneio. |
| `mapper.py` | Navega no Journal e liga cada título ao tipo que o ranking traz. |
| `mapper_ui.py` | A aba **Mapear torneios**. |
| `descobrir.py` | Janela do mapeador, com a aba **Diagnóstico** da Fase 0 (grava o tráfego e acha onde um dado está; use se o jogo mudar o formato). |

Formato confirmado em 12/09/2026:

- Lista do Journal (WebSocket): `[uuid, término, n, "global_tournament_user_result", [{blob: [[tipo, null, [tipo, variante], tem_detalhe]]}], ..., id_resultado]`
- Detalhe (WebSocket): `{id_resultado: [{blob: [["global_tournament_user_result", [tipo, variante], n, [[1, 1, [[[id_jogador], pontos], ...]]]]]}]}`
- Perfis (HTTP, rota 402): `[[id_jogador], conta, nome, país, ..., poder [10], ..., tag do clã [13]]`

**Abrir gravação...** reprocessa uma gravação feita pelo `descobrir.py`, sem
precisar do jogo; útil para testar depois de uma mudança.

## Versão

A versão mostrada no título da janela e enviada ao site (`client_version`)
vem de `version.py`, que **não se edita à mão**. Para lançar uma versão, faça
o commit com a versão no começo do título:

```
git commit -m "1.2.0"
git commit -m "1.2.0 correção do envio"
```

O hook `.githooks/post-commit` grava a versão em `version.py` e emenda o próprio
commit, então ele já sai com o app na versão nova. Commits cujo título não
começa com uma versão não mexem em nada. Num clone novo, ative o hook uma vez
(o `install.bat` já faz isso):

```
git config core.hooksPath .githooks
```

## Testes

```
.venv\Scripts\python -m unittest discover -s tests -v
```
