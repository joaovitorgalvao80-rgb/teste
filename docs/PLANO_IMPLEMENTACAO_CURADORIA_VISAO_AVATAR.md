# Plano de Implementacao: Curadoria Contextual, Visao de Video e Avatar Garantido

## Objetivo

Transformar o fluxo atual de B-roll em um sistema editorial verificavel:

- decidir melhor quando uma cena deve usar avatar, B-roll ou modo hibrido;
- gerar buscas mais praticas e menos dispersas;
- filtrar assets por contexto real, nao apenas por keyword;
- padronizar a quantidade de candidatos por cena;
- validar videos finalistas com frames extraidos;
- garantir que o avatar apareca no master final quando ele for obrigatorio.

O principio central: Railway deve continuar leve, como orquestrador. Render, composicao pesada e validacoes mais caras devem ficar no Kaggle ou em etapas muito limitadas.

## Problemas Que Este Plano Resolve

1. Keywords muito especificas, cientificas ou estranhas para bancos de stock.
2. Assets fora de contexto marcados como relevancia alta.
3. Cenas com excesso de candidatos e outras com poucos.
4. B-roll sendo usado em frases abstratas, transicoes ou falas que deveriam ficar no avatar.
5. Videos julgados apenas por thumbnail.
6. Avatar enviado, mas ausente no `final_master.mp4`.
7. Render considerado sucesso mesmo sem cumprir o contrato visual.

## Arquitetura Alvo

Cada cena deve ter um contrato editorial mais rico:

```json
{
  "screen_mode": "avatar_only | broll | hybrid | optional_broll",
  "visual_need": 0.0,
  "visual_strategy": "literal | evidence | environment | symbolic | none",
  "visual_target": "short concrete visual target",
  "must_have": [],
  "nice_to_have": [],
  "avoid": [],
  "query_ladder": [],
  "pool_policy": {
    "max_raw": 40,
    "max_visible": 12,
    "min_good": 6
  }
}
```

## Fase 1 - Classificador Editorial de Cena

Criar uma camada antes da busca para decidir se a cena realmente precisa de B-roll.

Saida esperada:

```json
{
  "screen_mode": "avatar_only",
  "visual_need": 0.12,
  "visual_strategy": "none",
  "reason": "frase de transicao, melhor manter apresentador"
}
```

Regras iniciais:

- objeto, acao ou local concreto: `broll`;
- dado visual ou demonstracao: `broll`;
- frase abstrata, opiniao, conclusao ou ponte: `avatar_only` ou `hybrid`;
- frase curta sem imagem natural: `avatar_only`;
- literalidade dificil demais: `optional_broll` com `visual_strategy = evidence`.

Arquivos provaveis:

- `services/groq_service.py`
- `services/edit_plan.py`
- `services/project_config.py`
- `database.py`
- `app_shared.py`

## Fase 2 - Novo Brief Visual Por Cena

Substituir o uso de `keywords` como centro do sistema por um brief visual estruturado.

Exemplo para a cena do mosquito:

```json
{
  "visual_target": "mosquito near stagnant water in bucket or backyard",
  "must_have": ["mosquito", "stagnant water"],
  "nice_to_have": ["bucket", "larvae", "backyard"],
  "avoid": ["bird", "lake", "generic insect swarm", "flower"],
  "query_ladder": [
    "mosquito stagnant water",
    "mosquito larvae water",
    "standing water bucket",
    "mosquito close up water"
  ]
}
```

Regras para `query_ladder`:

- usar termos buscaveis em Pexels/Pixabay/Coverr/Openverse/Wikimedia;
- evitar linguagem cientifica rara quando nao for necessaria;
- evitar termos singulares demais;
- priorizar evidencia visual quando o literal for dificil;
- nunca usar fallback generico que fuja do tema.

## Fase 3 - Busca Em Escada

Mudar `asset_search.search_scene` para buscar por etapas, nao misturar tudo de uma vez.

Fluxo:

1. buscar query principal;
2. avaliar se o pool ficou bom;
3. se ficou bom, reduzir buscas extras;
4. se ficou fraco, tentar alternativa;
5. se continuar fraco, tentar evidencia/contexto;
6. usar fallback/acervo so quando necessario.

Cada asset deve guardar:

- `query_role`: `primary`, `alternative`, `evidence`, `context`, `fallback`;
- `query_text`;
- `matched_terms`;
- `missing_terms`;
- `context_risks`.

Resultado esperado:

- menos assets dispersos;
- menos uso de fallback sem necessidade;
- melhor rastreabilidade de por que o asset apareceu.

## Fase 4 - Scoring Real de Contexto

Trocar o badge atual de relevancia por scores separados.

Novo modelo:

```json
{
  "context_score": 0.82,
  "quality_score": 0.71,
  "vision_score": 0.77,
  "final_score": 0.79,
  "matched": ["mosquito", "water"],
  "missing": ["bucket"],
  "risks": ["generic swarm"]
}
```

Regras:

- nao chamar de alta relevancia so porque veio da keyword certa;
- `must_have` pesa mais que qualidade tecnica;
- `avoid` derruba score forte;
- imagem bonita e fora de contexto deve perder;
- asset tecnicamente pior, mas correto, deve aparecer acima de asset bonito e errado.

Arquivos provaveis:

- `services/scoring.py`
- `services/auto_select.py`
- `services/vision.py`
- `templates/review.html`

## Fase 5 - Padronizacao de Pool Por Cena

Contrato inicial:

- pool bruto maximo: 40 assets;
- candidatos analisados: 8 a 12;
- candidatos visiveis por cena: maximo 12;
- minimo desejado: 8;
- se houver menos de 6 bons: marcar `pool_fraco`.

Distribuicao sugerida:

- ate 6 videos;
- ate 4 imagens;
- ate 2 acervo/fallback.

UI deve agrupar:

- recomendado;
- bons;
- alternativas;
- suspeitos/descartados.

## Fase 6 - Visao Com Prints de Video

Situacao atual:

- imagens: a IA analisa a imagem ou preview;
- videos: a IA analisa thumbnail/poster, nao o video inteiro;
- sem IA: heuristica por metadados e keyword.

Melhoria:

- usar thumbnail para triagem;
- para videos finalistas, extrair frames;
- analisar frames como sequencia;
- rejeitar video quando os frames nao sustentam o contexto.

Politica inicial:

- analisar frames apenas de finalistas;
- no maximo 2 ou 3 videos por cena;
- 1 frame para video curto;
- 3 frames para video medio/longo: 25%, 50%, 75%;
- timeout curto;
- fallback para thumbnail se falhar.

Saida esperada:

```json
{
  "video_frame_verdict": "descartar",
  "sampled_frames": 3,
  "reason": "frames mostram lago e aves, nao mosquito/agua parada"
}
```

Implementacao possivel:

- extrair frames com FFmpeg;
- salvar temporariamente no workdir;
- mandar os frames para Groq Vision/NVIDIA Vision;
- persistir resultado em campos de visao ou payload de auditoria.

## Fase 7 - Memoria de Rejeicao

Quando o usuario rejeitar asset, guardar motivo.

Motivos iniciais:

- fora de contexto;
- muito generico;
- baixa qualidade;
- nao mostra objeto;
- tipo errado;
- estetica ruim.

Uso:

- alimentar `avoid`;
- penalizar tags/autores/fontes repetidamente rejeitados;
- melhorar re-busca da mesma cena;
- impedir repetir o mesmo erro em nova rodada.

## Fase 8 - Curadoria Compacta

Cada card deve mostrar sinais objetivos:

- bateu: `mosquito + water`;
- faltou: `bucket`;
- risco: `generic swarm`;
- origem: `query primary/evidence/fallback`;
- visao: `otimo`, `bom`, `fraco`, `descartar`.

Isso reduz a carga manual e torna os erros auditaveis.

## Fase 9 - Avatar Como Contrato Obrigatorio

Problema: ja houve render bom com B-rolls e textos corretos, mas sem avatar.

Isso deve virar falha obrigatoria quando avatar for esperado.

Antes do pacote:

- se `video_style = avatar_broll`;
- e existe arquivo de avatar;
- entao `edit_plan.avatar` precisa existir;
- o ZIP precisa conter `avatar.mp4` ou `avatar.webm`;
- o `edit_plan.json` precisa apontar para o arquivo.

Antes do render:

- Kaggle precisa detectar `avatar_file`;
- `plan_avatar_mode` precisa retornar `base` ou `corner`;
- se avatar for obrigatorio, nunca pode cair silenciosamente em `none`.

Depois do render:

- `hyperframes_status.json` precisa registrar:
  - `requested_avatar: true`;
  - `avatar: true`;
  - `avatar_mode: base` ou `corner`.
- se `requested_avatar = true` e `avatar = false`, o job deve falhar.
- a UI nao pode mostrar master como sucesso.

## Fase 10 - Validacao Visual do Avatar

Adicionar validacao leve no `final_master.mp4`.

Fluxo:

1. extrair 3 frames do master;
2. recortar a regiao esperada do avatar;
3. verificar se ha conteudo visual ali;
4. se avatar nao aparece onde deveria, marcar falha.

Saida:

```json
{
  "avatar_visual_check": "failed",
  "sampled_frames": [2.0, 8.0, 14.0],
  "reason": "regiao do avatar sem conteudo detectavel"
}
```

Essa validacao pode comecar sem IA, usando diferenca visual/energia de pixels. Se necessario, evoluir para IA depois.

## Fase 11 - Fallback de Render Com Avatar

Se HyperFrames falhar, o fallback FFmpeg ainda precisa preservar avatar.

Regras:

- avatar-base: avatar como camada base;
- B-roll entra por cima nas janelas;
- textos entram por FFmpeg drawtext;
- audio vem da narracao ou avatar;
- se avatar obrigatorio falhar, o job falha.

Nada de liberar `final_master.mp4` sem avatar quando o avatar e obrigatorio.

## Ordem Recomendada de Implementacao

1. Corrigir contrato e validacao do avatar.
2. Corrigir scoring de contexto e badge de relevancia.
3. Criar classificador avatar/B-roll por cena.
4. Criar `visual_target`, `must_have`, `avoid`, `query_ladder`.
5. Implementar busca em escada.
6. Padronizar pool por cena.
7. Adicionar visao com prints dos videos finalistas.
8. Adicionar memoria de rejeicao.
9. Melhorar UI compacta da curadoria.
10. Adicionar testes/gates de render e diagnostico.

## Testes Obrigatorios

### Curadoria e busca

- cena de mosquito nao pode marcar garca/lago como alta relevancia;
- keyword impossivel deve virar estrategia de evidencia;
- cena abstrata deve virar avatar/hybrid;
- pool por cena deve respeitar maximo visivel;
- fallback nao deve rodar se primary ja tem bons candidatos.

### Visao

- imagem deve ser analisada diretamente;
- video deve usar thumbnail na triagem;
- video finalista deve extrair frames;
- video com frames fora de contexto deve ser descartado.

### Avatar/render

- pacote com avatar deve conter arquivo de avatar;
- `edit_plan.avatar` deve existir quando avatar e obrigatorio;
- runner Kaggle deve detectar avatar;
- `hyperframes_status.json` deve marcar avatar solicitado e presente;
- se avatar solicitado nao aparece, render falha;
- fallback FFmpeg tambem deve preservar avatar.

## Criterio de Sucesso

O sistema so deve ser considerado corrigido quando:

- B-roll for usado com discernimento editorial;
- cenas fracas/inuteis ficarem em avatar ou hibrido;
- keywords forem buscaveis e contextuais;
- assets fora de contexto nao ganharem relevancia alta;
- candidatos por cena forem padronizados;
- videos finalistas forem validados por frames reais;
- rejeicoes melhorarem a proxima busca;
- master final com avatar obrigatorio nunca for aprovado sem avatar visivel.

