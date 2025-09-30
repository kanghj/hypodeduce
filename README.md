# Hypodeduce: Abductive Fault Localization

## Description

This prototype uses a multi-phase approach:

1. **Hypothesis Generation**: Creates multiple hypotheses about potential root causes of test failures
2. **Class Ranking**: Pre-ranks and scores covered classes based on failure context
3. **Method Analysis**: Evaluates individual methods using enhanced documentation and source code

The pipeline uses structured prompts to query GPT models at each phase, tracks token usage, and produces output in a csv file.

## Requirements

- Python 3.8+
- OpenAI API key (set as `OPENAI_API_KEY` environment variable)
- Required Python packages:
  - `openai`


## Usage

```bash
python src/run_gpt_pipeline.py Chart-22 \
  --facts-dir soapfl_facts \
  --output-dir results/Chart-22 \
  --max-hypotheses 3 \
  --classes-per-hypothesis 4 \
  --methods-per-class 4 \
  --model gpt-4o
```

