# Проверки блока Б

Контрактные тесты находятся в `tests/contract/test_communities_and_search.py`.
Дополнительные тесты разделены по функциям:

- `test_communities.py` - разбиение графа, сводки, веса и связи между сообществами,
  в том числе передача сводок и источников в global;
- `test_local_search.py` - локальные факты, источники, отказы и циклы замен;
- `test_global_search.py` - обработка некорректных оценок и источников global.

Из корня проекта: `make test ARGS="tests/contract/test_communities_and_search.py tests/block_b"`.
Для локального окружения вместо контейнера: `make test RUN="uv run" ARGS="tests/contract/test_communities_and_search.py tests/block_b"`.
Обычный `make test` тоже собирает эти тесты. `FakeLLM` проверяет поведение кода,
но не качество реальных ответов модели; для этого нужны отдельные прогоны оценки.
