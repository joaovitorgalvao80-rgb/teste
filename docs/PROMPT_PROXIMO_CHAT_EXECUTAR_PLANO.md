# Prompt Para Proximo Chat

Use este prompt no novo chat:

```text
Estamos no projeto NWRCH Studio em:
D:\NewProjectCloud\b-rolls-automatic\sistema1_plataforma\deploy

Leia primeiro o arquivo:
docs/PLANO_IMPLEMENTACAO_CURADORIA_VISAO_AVATAR.md

Esse arquivo contem o plano completo para resolver, de forma integrada:
1. busca de assets dispersa e sem contexto;
2. keywords ruins ou dificeis demais para bancos de stock;
3. decisao melhor entre avatar, B-roll, hybrid e optional_broll;
4. padronizacao de quantidade/qualidade de candidatos por cena;
5. visao com analise de imagens e prints extraidos de videos finalistas;
6. memoria de rejeicao;
7. bug recorrente em que o master final sai sem avatar mesmo quando deveria ter avatar.

Nao comece implementando no escuro.
Primeiro leia o plano inteiro, audite o codigo atual e derive uma ordem segura de execucao.

Depois implemente o plano por fases, com prioridade para:
1. contrato e validacao obrigatoria do avatar no pacote/render/master;
2. scoring real de contexto e badge de relevancia;
3. classificador avatar vs B-roll por cena;
4. query_ladder e busca em escada;
5. padronizacao do pool por cena;
6. frame sampling para videos finalistas;
7. memoria de rejeicao e UI de curadoria compacta.

Regras importantes:
- Railway deve continuar leve; nao colocar render pesado ou analise massiva nele.
- Kaggle continua sendo o worker de render.
- Nao considerar render como sucesso se avatar era obrigatorio e nao aparece.
- Nao chamar asset de alta relevancia so porque veio da keyword certa.
- Testar com contratos reais do repo.
- Depois de alterar `services/kaggle_service.py`, compilar tambem o runner embutido com:
  compile(kaggle_service._RUNNER, "runner.py", "exec")
- Rodar testes relevantes e reportar evidencias concretas.
```

