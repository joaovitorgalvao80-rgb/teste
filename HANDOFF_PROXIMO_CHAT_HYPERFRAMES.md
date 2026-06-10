# Handoff para o proximo chat - NWRCH Studio + HyperFrames

## Primeiro passo obrigatorio

Leia este arquivo inteiro antes de propor ou implementar qualquer coisa:

`D:\NewProjectCloud\b-rolls-automatic\sistema1_plataforma\deploy\HANDOFF_PROXIMO_CHAT_HYPERFRAMES.md`

Nao comece do zero. O projeto ja tem uma versao funcional, testada, com backup feito. A partir daqui, a prioridade e evoluir com seguranca para o fluxo de refinamento com HyperFrames.

## Contexto geral

O projeto fica em:

`D:\NewProjectCloud\b-rolls-automatic\sistema1_plataforma\deploy`

Repositorio GitHub:

`https://github.com/joaovitorgalvao80-rgb/teste.git`

Branch principal:

`main`

Commit funcional atual antes da etapa HyperFrames:

`a33e14b Add NWRCH Studio visual identity`

Esse commit contem:

- fluxo funcional de render no Kaggle;
- nova identidade visual NWRCH Studio;
- UI dark premium;
- testes de contrato passando;
- `.gitignore` protegendo `data/`, `workdir/`, caches e logs.

## Backup feito antes da nova etapa

Antes de mexer em HyperFrames, foi feito um backup limpo do projeto versionado no commit `a33e14b`.

Copia:

`D:\NewProjectCloud\backups\nwrch-studio-deploy-working-20260609-225834-a33e14b`

ZIP:

`D:\NewProjectCloud\backups\nwrch-studio-deploy-working-20260609-225834-a33e14b.zip`

Validacao do backup:

- commit congelado: `a33e14b`;
- copia com 24 arquivos;
- ZIP com 24 arquivos;
- tamanho do ZIP: `49,313 bytes`;
- backup criado a partir do Git, sem incluir `data/`, banco local, screenshots, caches ou chaves runtime.

## Estado funcional atual

O sistema ja consegue:

1. Criar conta local e configurar APIs.
2. Criar projeto com roteiro/timestamps.
3. Transcrever audio com Groq Whisper.
4. Gerar mapa visual.
5. Buscar assets em Pexels/Pixabay.
6. Selecionar/rejeitar/favoritar assets por cena.
7. Preparar pacote ZIP.
8. Enviar pacote para Kaggle.
9. Renderizar base de b-roll no Kaggle.
10. Detectar output pronto mesmo sem depender do endpoint quebrado do Kaggle.
11. Baixar video do output do Kaggle.
12. Servir o video baixado no app.

O usuario testou e confirmou que funcionou:

> "FUNCIONOU, O VIDEO FICOU PRONTO E MUITO RAPIDO"

## Problemas do Kaggle ja resolvidos

Foram corrigidas causas reais que quebravam o fluxo:

- o app nao depende mais de `GetKernelSessionStatus`, que retornava erro 500;
- o runner do Kaggle agora aceita quando o Kaggle descompacta o ZIP como pasta;
- o app consegue baixar o MP4 via `kaggle kernels output` quando nao existe URL direta;
- o parser de arquivos do Kaggle nao confunde cabecalho CSV com arquivo real;
- o status do Kaggle ficou mais resiliente a falhas transitorias.

## Testes ja realizados

Testes automatizados:

- `python -m unittest discover -v`: 14 testes OK.
- `python -m compileall app.py database.py montador.py services tests`: OK.
- `git diff --check`: OK.

Testes reais feitos anteriormente:

- Pexels API respondeu corretamente.
- Pixabay API respondeu corretamente.
- Groq API respondeu corretamente.
- Kaggle autenticou.
- Fluxo local completo renderizou video com FFmpeg.
- Smoke real no Kaggle passou depois da correcao do runner.

Nao incluir chaves de API em arquivos, commits ou respostas. O usuario forneceu chaves temporarias em conversa anterior, mas elas nao devem ser repetidas.

## Identidade visual atual

O sistema deixou de se chamar "B-rolls Curadoria" e passou a se chamar:

`NWRCH Studio`

Subtitulo visual:

`AI B-roll Production`

Direcao visual:

- dark premium;
- carbono/preto;
- mint/ciano;
- lime/accent;
- cara de studio/editor/control room;
- cards com raio curto;
- sem landing page;
- primeira tela como ferramenta real.

Arquivos principais alterados na etapa visual:

- `app.py`
- `static/style.css`
- `static/gallery.js`
- `templates/base.html`
- `templates/login.html`
- `templates/projects.html`
- `templates/project.html`
- `templates/new_project.html`
- `templates/settings.html`
- `templates/error.html`

O commit da identidade visual foi:

`a33e14b Add NWRCH Studio visual identity`

## Ideia arquitetural discutida

O usuario quer evoluir o produto para duas etapas:

### 1. Base do video

Gerar somente o video base com b-rolls:

- sem audio;
- sem avatar;
- sem refinamento pesado;
- apenas assets encaixados na ordem certa;
- duracoes corretas por cena;
- estrutura visual base pronta.

Possivel nome de output:

`base_broll.mp4`

Essa parte pode continuar com Kaggle, porque ja funciona bem.

### 2. Refinamento / Master

Depois da base pronta, gerar o video final refinado:

- adicionar audio/narracao;
- sobrepor avatar;
- aplicar cortes;
- aplicar transicoes;
- adicionar motion/zoom/pan;
- adicionar legendas/captions se fizer sentido;
- gerar output final publicavel.

Possivel nome de output:

`final_master.mp4`

Essa etapa deve usar HyperFrames como motor de composicao/render refinado.

## Papel de cada tecnologia

### Kaggle

Fica como renderizador da base visual:

- monta b-rolls;
- encaixa cenas;
- exporta video bruto/mudo;
- barato e ja validado.

### HyperFrames

Entra como camada de refinamento/master:

- pega `base_broll.mp4`;
- pega audio/narracao;
- pega avatar;
- pega roteiro/timestamps;
- pega um plano de edicao;
- gera composicao HTML;
- renderiza `final_master.mp4`.

### OpenRouter / NVIDIA

Entram como cerebro editorial para texto/planejamento:

- gerar `edit_plan.json`;
- decidir cortes;
- decidir transicoes;
- decidir momentos de motion;
- decidir onde avatar aparece;
- decidir captions/lower thirds;
- melhorar mapa visual e keywords no futuro.

### Groq

Continuar forte para:

- Whisper/transcricao;
- tarefas rapidas.

Nao substituir Groq Whisper sem motivo; para audio ele ja e util.

## O que foi lido sobre HyperFrames

Documentacao oficial:

- `https://hyperframes.mintlify.app/introduction`
- `https://hyperframes.mintlify.app/quickstart`
- `https://hyperframes.mintlify.app/concepts/data-attributes`
- `https://hyperframes.mintlify.app/concepts/variables`
- `https://hyperframes.mintlify.app/packages/cli`
- `https://hyperframes.mintlify.app/packages/producer`
- `https://hyperframes.mintlify.app/guides/rendering`
- `https://hyperframes.mintlify.app/guides/remove-background`
- `https://hyperframes.mintlify.app/guides/deploy`

GitHub:

`https://github.com/heygen-com/hyperframes`

Pontos importantes da documentacao:

- HyperFrames e open-source.
- Ele transforma HTML/CSS/media/animacoes em video.
- Composicoes sao HTML com atributos `data-*`.
- Atributos importantes:
  - `data-composition-id`
  - `data-width`
  - `data-height`
  - `data-start`
  - `data-duration`
  - `data-track-index`
  - `data-media-start`
  - `data-volume`
  - `data-has-audio`
- Usa render frame-by-frame com Chrome/headless e FFmpeg.
- CLI e boa para automacao:
  - `npx hyperframes lint`
  - `npx hyperframes snapshot`
  - `npx hyperframes inspect`
  - `npx hyperframes render`
  - `npx hyperframes doctor`
- Renderiza MP4, WebM, MOV e PNG sequence.
- Suporta variaveis com `--variables` ou `--variables-file`.
- Isso combina muito bem com um `edit_plan.json`.
- Tem `remove-background` local para avatar transparente:
  - WebM com alpha;
  - MOV ProRes 4444;
  - PNG para imagem.
- Pode rodar local, Docker, ou em deploys/cloud templates.

## Hipotese de implementacao

A implementacao deve ser incremental e segura.

### Fase 1 - Preparar estrutura sem quebrar fluxo atual

Adicionar no app o conceito de dois outputs:

- base b-roll;
- master final.

Possiveis campos no banco/projeto:

- `base_video_path` ou usar localizacao no `WORK_DIR`;
- `master_video_path`;
- `hyperframes_status`;
- `hyperframes_project_dir`;
- `edit_plan_json`.

Evitar migracao grande demais se der para iniciar com arquivos no `WORK_DIR/project_<id>/`.

### Fase 2 - Criar edit plan

Criar um servico para gerar ou montar um `edit_plan.json`.

Primeira versao pode ser deterministica/manual, sem IA:

- usar cenas ja existentes;
- usar timestamps;
- posicionar avatar com base em `avatar_safe_area`;
- usar transicoes simples;
- usar motion simples;
- usar audio se fornecido.

Depois conectar OpenRouter/NVIDIA para gerar versao mais inteligente.

Exemplo conceitual:

```json
{
  "version": 1,
  "resolution": "1920x1080",
  "fps": 30,
  "base_video": "base_broll.mp4",
  "audio": {
    "src": "narration.wav",
    "volume": 1.0
  },
  "avatar": {
    "src": "avatar.webm",
    "position": "right",
    "scale": 0.32,
    "start": 0,
    "duration": 60
  },
  "scenes": [
    {
      "scene_id": "scene_001",
      "start": 0,
      "duration": 4.2,
      "motion": "slow_push_in",
      "transition_out": "fade",
      "caption": "Texto curto da cena"
    }
  ]
}
```

### Fase 3 - Gerar projeto HyperFrames

Criar algo como:

`services/hyperframes_service.py`

Responsabilidades:

- criar pasta `WORK_DIR/project_<id>/hyperframes/`;
- escrever `index.html`;
- escrever `variables.json`;
- copiar/referenciar `base_broll.mp4`;
- copiar/referenciar audio;
- copiar/referenciar avatar;
- rodar comandos HyperFrames.

Possiveis comandos:

```powershell
npx hyperframes lint
npx hyperframes inspect --json
npx hyperframes snapshot --frames 5
npx hyperframes render --output final_master.mp4 --quality standard
```

### Fase 4 - UI

Adicionar na tela do projeto uma nova etapa depois do Kaggle:

`05 Refinar com HyperFrames`

Estados desejados:

- `base pronta`;
- `refinando`;
- `master pronto`;
- `erro no refinamento`.

Botoes:

- `Baixar base b-roll`;
- `Refinar com HyperFrames`;
- `Baixar video final`;
- talvez `Abrir diagnostico`.

### Fase 5 - Fallback

Se HyperFrames falhar:

- nao perder o video base;
- manter link para baixar base;
- mostrar erro claro;
- permitir tentar novamente;
- preservar logs.

## Cuidado importante

Nao transformar HyperFrames em substituto imediato do Kaggle.

Fluxo desejado:

```text
Roteiro
  -> mapa visual
  -> assets
  -> Kaggle gera base_broll.mp4
  -> edit_plan.json
  -> HyperFrames gera final_master.mp4
```

A base atual funciona. Nao quebrar essa base.

## Regras praticas para o proximo agente

1. Leia este arquivo inteiro antes de agir.
2. Verifique `git status` antes de modificar.
3. Nao inclua chaves/API em arquivos ou commits.
4. Nao mexa no fluxo Kaggle funcional sem necessidade.
5. Implemente em etapas pequenas.
6. Rode testes depois de cada mudanca relevante.
7. Se precisar instalar Node/HyperFrames/dependencias, pedir permissao antes.
8. Preferir uma primeira integracao minima funcionando a uma arquitetura gigante.
9. Manter o app utilizavel mesmo se HyperFrames falhar.
10. Commitar/pushar so depois de validar, se o usuario pedir ou se estiver claro que e para subir.

## Prompt sugerido para iniciar o novo chat

Copie e cole o texto abaixo no novo chat:

```text
Estamos continuando o projeto NWRCH Studio em:

D:\NewProjectCloud\b-rolls-automatic\sistema1_plataforma\deploy

Antes de qualquer coisa, leia este arquivo inteiro:

D:\NewProjectCloud\b-rolls-automatic\sistema1_plataforma\deploy\HANDOFF_PROXIMO_CHAT_HYPERFRAMES.md

Nao comece do zero. Esse arquivo resume tudo que ja aconteceu, o estado funcional atual, o backup feito, o commit atual, os problemas Kaggle ja resolvidos e a arquitetura que queremos seguir.

Contexto rapido:

O app ja funciona com Kaggle. Ele gera a base de b-roll, envia para Kaggle, renderiza, detecta output e baixa o MP4. O usuario confirmou que funcionou rapido. Tambem ja existe a UI nova chamada NWRCH Studio, com commit a33e14b.

Antes de mexer em HyperFrames, foi criado backup limpo:

D:\NewProjectCloud\backups\nwrch-studio-deploy-working-20260609-225834-a33e14b
D:\NewProjectCloud\backups\nwrch-studio-deploy-working-20260609-225834-a33e14b.zip

Agora queremos planejar/implementar a proxima evolucao:

1. Manter Kaggle como gerador da base visual:
   - base_broll.mp4
   - somente b-rolls
   - sem audio
   - sem avatar
   - sem refinamento pesado

2. Adicionar HyperFrames como etapa de refinamento/master:
   - pegar base_broll.mp4
   - adicionar audio/narracao
   - sobrepor avatar
   - aplicar cortes, transicoes, motions, captions/lower thirds
   - gerar final_master.mp4

3. Usar OpenRouter/NVIDIA futuramente como cerebro editorial:
   - gerar edit_plan.json
   - escolher cortes, transicoes, motions e captions

4. Manter Groq principalmente para Whisper/transcricao e tarefas rapidas.

Importante:

- Nao incluir nenhuma chave de API em arquivos, commits ou respostas.
- Nao quebrar o fluxo atual do Kaggle.
- Trabalhar incrementalmente.
- Primeiro validar o ambiente HyperFrames/Node/FFmpeg.
- Se precisar instalar dependencias ou usar rede, pedir permissao.
- Criar uma primeira versao minima: gerar projeto HyperFrames a partir de uma base local/video base e renderizar um master simples.
- Depois expandir para edit_plan.json, avatar, audio e UI.

Quero que voce primeiro leia o arquivo .md, analise o projeto, confirme o estado atual com git status, e so entao me proponha o plano de implementacao em fases. Nao implemente nada antes de me explicar o plano e eu aprovar.
```

