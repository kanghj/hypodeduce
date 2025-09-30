#!/usr/bin/env python3
"""Run the hypothesis-driven FL pipeline using direct GPT calls (no Scallop)."""

import argparse
import csv
import json
import math
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.append('src')

import openai

from facts_data import load_facts_from_json


DEFAULT_MODEL = "gpt-4o"
TEMPERATURE = 0.1
MAX_HYPOTHESES = 3
CLASSES_PER_HYPOTHESIS = 4
METHODS_PER_CLASS = 4
LARGE_CLASS_THRESHOLD = 30
LARGE_CLASS_PREFILTER_LIMIT = 30


def truncate_text(text: str, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    suffix = "..."
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


def summarize_list(items: Sequence[str], limit: int = 6) -> str:
    filtered = [item for item in items if item]
    if not filtered:
        return ""
    if len(filtered) <= limit:
        return ", ".join(filtered)
    remaining = len(filtered) - limit
    return ", ".join(filtered[:limit]) + f", ... (+{remaining})"


def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_failure_components(facts: Dict[str, Any]) -> Dict[str, Any]:
    failed_test = facts.get("failed_test") or {}
    test_suite = failed_test.get("test_class") or facts.get("test_suite") or "unknown_test_suite"
    test_name = failed_test.get("name") or "unknown_test"
    test_method = failed_test.get("test_method") or "unknown_method"
    display = failed_test.get("display_name") or test_name

    components = {
        "test_suite": test_suite,
        "test_name": test_name,
        "test_method": test_method,
        "failed_tests": display,
    }

    components["code"] = truncate_text(failed_test.get("code", ""), 700)
    components["stack_trace"] = truncate_text(failed_test.get("stack_trace", ""), 500)
    components["output"] = truncate_text(failed_test.get("test_output", ""), 400)
    components["test_behavior"] = truncate_text(facts.get("test_behavior", ""), 500)

    summary_parts = []
    if components["test_behavior"]:
        summary_parts.append(f"Test behavior analysis:\n{components['test_behavior']}")
    if components["code"]:
        summary_parts.append(f"Test code excerpt:\n{components['code']}")
    if components["stack_trace"]:
        summary_parts.append(f"Stack trace excerpt:\n{components['stack_trace']}")
    if components["output"]:
        summary_parts.append(f"Test output excerpt:\n{components['output']}")
    components["summary"] = "\n\n".join(summary_parts)

    assertion_error = facts.get("assertion_error") or failed_test.get("assertion_error") or {}
    if assertion_error:
        components["assertion"] = (
            f"Expected: {assertion_error.get('expected', '')}; Actual: {assertion_error.get('actual', '')}"
        )
    else:
        components["assertion"] = ""

    return components


class GPTClient:
    def __init__(self, model: str = DEFAULT_MODEL, temperature: float = TEMPERATURE, retries: int = 3):
        self.model = model
        self.temperature = temperature
        self.retries = retries
        self.client = openai.OpenAI()
        self.total_tokens = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0

    def call(self, prompt: str, label: str = "") -> str:
        system_msg = {
            "role": "system",
            "content": "You are an experienced software engineer who responds concisely and follows instructions exactly.",
        }
        user_msg = {"role": "user", "content": prompt}

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    messages=[system_msg, user_msg],
                )
                content = response.choices[0].message.content or ""
                
                # Track token usage
                if hasattr(response, 'usage') and response.usage:
                    self.total_tokens += response.usage.total_tokens
                    self.total_prompt_tokens += response.usage.prompt_tokens
                    self.total_completion_tokens += response.usage.completion_tokens
                    self.call_count += 1
                    
                    print(f"  📊 GPT[{label}] tokens: {response.usage.prompt_tokens} prompt + {response.usage.completion_tokens} completion = {response.usage.total_tokens} total")
                
                return content.strip()
            except Exception as exc:  # broad catch to surface errors
                last_error = exc
                wait = 2 ** (attempt - 1)
                print(f"  ⚠️  GPT[{label}] attempt {attempt} failed: {exc}. Retrying in {wait}s")
                time.sleep(wait)

        raise RuntimeError(f"GPT call '{label}' failed after {self.retries} attempts") from last_error
    
    def get_token_summary(self) -> Dict[str, int]:
        """Get summary of token usage."""
        return {
            "total_calls": self.call_count,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens
        }


def coerce_float(text: Any) -> Optional[float]:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if isinstance(text, str):
        stripped = text.strip()
        if not stripped:
            return None
        import re

        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", stripped)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def parse_score_and_reason(raw: str) -> Tuple[Optional[float], str]:
    if not isinstance(raw, str) or not raw.strip():
        return None, ""
    score = coerce_float(raw.split("\n", 1)[0])
    return score, raw.strip()


def parse_ranked_methods(raw: str, methods: List[str]) -> Tuple[Dict[str, float], Dict[str, str]]:
    scores: Dict[str, float] = {}
    rationales: Dict[str, str] = {}
    if not raw:
        print(f"    ⚠️  Empty tie-breaking response")
        return scores, rationales

    raw = raw.strip()
    attempts = [raw]
    import re

    match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
    if match:
        attempts.append(match.group(1))

    parsed_obj: Any = None
    for i, candidate in enumerate(attempts):
        try:
            parsed_obj = json.loads(candidate)
            print(f"    ✅ Successfully parsed JSON attempt {i+1}")
            break
        except Exception as e:
            print(f"    ❌ JSON parse attempt {i+1} failed: {e}")
            continue
    
    if parsed_obj is None:
        print(f"    ❌ Failed to parse any JSON from response")
        return scores, rationales

    print(f"    🔍 Parsed object type: {type(parsed_obj)}")
    print(f"    🔍 Parsed object content: {parsed_obj}")

    def record(method_name: str, value: Any, rationale: Any = "") -> None:
        if method_name not in methods:
            print(f"    ⚠️  Method '{method_name}' not in expected methods list")
            return
        if method_name in scores:
            print(f"    ⚠️  Method '{method_name}' already processed, skipping")
            return
        print(f"    🔍 Processing method: {method_name}, value: {value} (type: {type(value)})")
        numeric = coerce_float(value)
        print(f"    🔍 Coerced to: {numeric}")
        if numeric is None:
            numeric = 0.0
        final_score = max(float(numeric), 0.0)
        scores[method_name] = final_score
        print(f"    📝 Recorded {method_name}: {numeric} -> {final_score}")
        if isinstance(rationale, str) and rationale.strip():
            rationales[method_name] = rationale.strip()

    if isinstance(parsed_obj, list):
        for entry in parsed_obj:
            if isinstance(entry, dict):
                name = entry.get("method") or entry.get("name") or entry.get("candidate")
                score = entry.get("score") or entry.get("tie_break_score")
                record(name, score, entry.get("rationale") or entry.get("reason"))
            elif isinstance(entry, str):
                record(entry, None, "")
    elif isinstance(parsed_obj, dict):
        ranking = parsed_obj.get("ranking")
        if isinstance(ranking, list):
            for entry in ranking:
                if isinstance(entry, dict):
                    name = entry.get("method") or entry.get("name") or entry.get("candidate")
                    score = entry.get("score") or entry.get("tie_break_score")
                    record(name, score, entry.get("rationale") or entry.get("reason"))
                elif isinstance(entry, str):
                    record(entry, None, "")
        else:
            for key, value in parsed_obj.items():
                if key in methods:
                    rationale = value.get("rationale") if isinstance(value, dict) else ""
                    score_value = value.get("score") if isinstance(value, dict) else value
                    record(key, score_value, rationale)

    # Fallback: use order of appearance
    if not scores:
        lowered = raw.lower()
        occurrences = [(lowered.find(m.lower()), m) for m in methods if m.lower() in lowered]
        occurrences = [item for item in occurrences if item[0] != -1]
        occurrences.sort()
        for idx, (_, method) in enumerate(occurrences):
            scores[method] = (len(occurrences) - idx) / max(len(occurrences), 1)

    if not scores:
        scores = {m: 0.0 for m in methods}

    max_score = max(scores.values()) if scores else 0.0
    if max_score > 0:
        for method in list(scores.keys()):
            scores[method] = scores[method] / max_score

    return scores, rationales


def generate_hypotheses(
    gpt: GPTClient,
    components: Dict[str, Any],
    max_hypotheses: int,
) -> List[Dict[str, Any]]:
    hypotheses: List[Dict[str, Any]] = []
    for idx in range(1, max_hypotheses + 1):
        label = f"H{idx}"
        prompt = textwrap.dedent(
            f"""
            [REL=hypothesis_generation]
            You are analysing failing test "{components['failed_tests']}".
            Hypothesis {label}: propose a distinct potential root cause for the failure.
            Describe it in one sentence.
            """
        ).strip()
        text = gpt.call(prompt, f"hypothesis {label}")
        if not text:
            continue
        hypotheses.append({"id": label, "text": text.strip()})

    for hyp in hypotheses:
        prompt = textwrap.dedent(
            f"""
            [REL=hypothesis_confidence]
            Given Hypothesis {hyp['id']}: {hyp['text']}.
            Considering the same failing test context, rate your confidence (0-1).
            Return only a number.
            """
        ).strip()
        score_text = gpt.call(prompt, f"hypothesis_confidence {hyp['id']}")
        hyp["confidence"] = coerce_float(score_text) or 0.0

    return hypotheses


def filter_covered_classes(facts: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    covered_classes = facts.get('covered_classes') or []
    covered_methods = facts.get('covered_methods') or []
    class_methods = facts.get('class_methods') or {}

    classes_with_methods = set()
    for method in covered_methods:
        if isinstance(method, str) and '.' in method:
            classes_with_methods.add(method.rsplit('.', 1)[0])
    for cls, methods in class_methods.items():
        if methods:
            classes_with_methods.add(cls)

    filtered = [cls for cls in covered_classes if cls in classes_with_methods]
    excluded = [cls for cls in covered_classes if cls not in classes_with_methods]
    return filtered, excluded


def pre_rank_classes(
    gpt: GPTClient,
    classes: List[str],
    facts: Dict[str, Any],
    components: Dict[str, Any],
    limit: int,
) -> List[Tuple[str, Optional[float], str]]:
    class_methods = facts.get('class_methods') or {}
    covered_methods = facts.get('covered_methods') or []
    covered_classes_text = summarize_list(classes, limit=25)

    prompts: Dict[str, str] = {}
    for cls in classes:
        lines = [
            f"One or more tests in the test suite \"{components['test_suite']}\" failed due to a single bug.",
            f"Failed tests: \"{components['failed_tests']}\"",
            "According to the test code, error stack trace, and outputs provided below:",
            components['summary'],
            "---",
            f"Covered Classes List: {covered_classes_text}",
            f"Class under evaluation: {cls}",
        ]
        methods = class_methods.get(cls, [])
        if methods:
            lines.append("Declared methods: " + summarize_list([str(m) for m in methods], limit=10))
        covered = [m for m in covered_methods if isinstance(m, str) and m.startswith(f"{cls}.")]
        if covered:
            lines.append("Covered methods in failing execution: " + summarize_list([m.split('.')[-1] for m in covered]))
        doc = facts.get('class_documentation', {}).get(cls)
        if doc:
            lines.append("Class documentation excerpt:\n" + truncate_text(str(doc), 220))
        lines.append("As the Defect Prioritizer, estimate how likely this class is the best location to fix the bug.")
        lines.append("Return JSON {\"score\": <0-1 float>, \"reason\": \"<=2 sentences referencing the failure evidence\"}.")
        prompts[cls] = "[REL=class_pre_ranking]\n" + "\n".join(lines)

    print(f"    ▶️ GPT[class pre-ranking] running {len(prompts)} prompts")
    responses = {cls: gpt.call(prompt, f"class_pre_rank {cls}") for cls, prompt in prompts.items()}
    scored: List[Tuple[str, Optional[float], str]] = []
    for cls in classes:
        score, reason = parse_score_and_reason(responses.get(cls, ""))
        scored.append((cls, score, reason))

    scored_with = [entry for entry in scored if entry[1] is not None]
    scored_without = [entry for entry in scored if entry[1] is None]
    scored_with.sort(key=lambda entry: entry[1], reverse=True)
    scored_without.sort(key=lambda entry: classes.index(entry[0]))
    ordered = scored_with + scored_without
    print(f"    ✅ GPT[class pre-ranking] completed; taking top {limit}")
    return ordered[:limit]


def gather_methods_for_classes(
    classes: List[str],
    facts: Dict[str, Any],
    covered_methods: List[str],
) -> List[str]:
    class_methods = facts.get('class_methods') or {}
    method_source_code = facts.get('method_source_code') or {}
    methods: List[str] = []

    # Add methods from class_methods (existing logic)
    for cls in classes:
        for method in class_methods.get(cls, []):
            full = method if isinstance(method, str) and method.startswith(f"{cls}.") else f"{cls}.{method}"
            methods.append(full)

        # Also add from inner classes (e.g., Class$InnerClass)
        for class_name in class_methods.keys():
            if class_name.startswith(f"{cls}$"):  # Inner class
                for method in class_methods.get(class_name, []):
                    full = f"{class_name}.{method}"
                    methods.append(full)

    # Add any method with source code for selected classes (including inner classes)
    for method_name in method_source_code.keys():
        if any(method_name.startswith(f"{cls}.") or method_name.startswith(f"{cls}$") for cls in classes):
            methods.append(method_name)

    # Add covered methods (existing logic)
    for method in covered_methods:
        if isinstance(method, str) and '.' in method:
            method_class = method.rsplit('.', 1)[0]
            if method_class in classes:
                methods.append(method)

    methods.sort()
    return dedupe_preserve_order(methods)


def prefilter_large_classes(
    gpt: GPTClient,
    class_method_map: Dict[str, List[str]],
    components: Dict[str, Any],
) -> Dict[str, List[str]]:
    if not class_method_map:
        return {}
    prompts: Dict[str, str] = {}
    for cls, methods in class_method_map.items():
        if not methods:
            continue
        bullet_list = "\n".join(f"- {method}" for method in methods)
        prompt = textwrap.dedent(
            f"""
            [REL=large_class_prefilter]
            Failure summary: {components['summary']}
            Focus class: {cls}
            Candidate methods (names only):
            {bullet_list}
            Rank the methods by likelihood of containing the defect in the failing test.
            Return JSON array; each entry should provide the exact method name from the list and a numeric 'score' between 0 and 1.
            """
        ).strip()
        prompts[cls] = prompt

    results: Dict[str, List[str]] = {}
    for cls, prompt in prompts.items():
        print(f"    ▶️ GPT[large class prefilter] {cls} handling {len(class_method_map[cls])} methods")
        raw = gpt.call(prompt, f"large_class_prefilter {cls}")
        scores, _ = parse_ranked_methods(raw, class_method_map[cls])
        ordered = sorted(class_method_map[cls], key=lambda m: scores.get(m, 0.0), reverse=True)
        results[cls] = ordered[:LARGE_CLASS_PREFILTER_LIMIT]
        print(
            f"    ✅ Prefiltered {cls}: keeping {len(results[cls])}/{len(class_method_map[cls])} methods"
        )
    return results


def pre_rank_methods(
    gpt: GPTClient,
    method_pool: List[str],
    facts: Dict[str, Any],
    components: Dict[str, Any],
) -> Tuple[List[Tuple[str, Optional[float], str]], Dict[str, Dict[str, Any]]]:
    if not method_pool:
        return [], {}

    source_map = facts.get('method_source_code') or {}
    class_docs = facts.get('class_documentation') or {}
    method_docs = facts.get('method_documentation') or {}
    method_data: Dict[str, Dict[str, Any]] = {}
    for method in method_pool:
        method_class = method.rsplit('.', 1)[0] if '.' in method else ""
        simple = method.split('.')[-1] if '.' in method else method
        method_data[method] = {
            "class": method_class,
            "simple": simple,
            "source": source_map.get(method),
            "class_doc": class_docs.get(method_class),
            "method_doc": method_docs.get(method),
        }

    prompts: Dict[str, str] = {}
    for method in method_pool:
        data = method_data[method]
        lines = [
            f"One or more tests in the test suite \"{components['test_suite']}\" failed due to a single bug.",
            f"Failed tests: \"{components['failed_tests']}\"",
            "According to the failure evidence below:",
            components['summary'],
            "---",
            "As the Method Reviewer, determine whether this method is the best location to fix the bug.",
            f"Method under review: {method}",
            f"Declaring class: {data['class'] or 'unknown'}",
        ]
        class_doc = truncate_text(str(data.get('class_doc') or ''), 220)
        if class_doc:
            lines.append("Class documentation excerpt:\n" + class_doc)

        # Use enhanced method documentation if available, otherwise fall back to source code
        method_doc = data.get('method_doc')
        if method_doc:
            lines.append("Method summary (enhanced):\n" + truncate_text(str(method_doc), 400))

        source = data.get('source')
        if source:
            lines.append("Source snippet:\n" + truncate_text(source, 400))
        else:
            lines.append("Source snippet: (not available)")
        lines.append("Return JSON {\"score\": <0-1 float>, \"reason\": \"<=3 sentences referencing the failure evidence\"}.")
        prompts[method] = "[REL=method_pre_ranking]\n" + "\n".join(lines)

    print(f"    ▶️ GPT[method pre-ranking] running {len(prompts)} prompts")
    responses = {method: gpt.call(prompt, f"method_pre_rank {method}") for method, prompt in prompts.items()}
    scored: List[Tuple[str, Optional[float], str]] = []
    for method in method_pool:
        score, reason = parse_score_and_reason(responses.get(method, ""))
        scored.append((method, score, reason))

    scored_with = [entry for entry in scored if entry[1] is not None]
    scored_without = [entry for entry in scored if entry[1] is None]
    scored_with.sort(key=lambda entry: entry[1], reverse=True)
    scored_without.sort(key=lambda entry: method_pool.index(entry[0]))
    ordered = scored_with + scored_without
    print(f"    ✅ GPT[method pre-ranking] completed")
    return ordered, method_data


def select_candidate_methods(
    ordered_methods: List[Tuple[str, Optional[float], str]],
    method_data: Dict[str, Dict[str, Any]],
    top_classes: List[str],
    methods_per_class: int,
    max_hypotheses: int,
) -> List[str]:
    methods_by_class: Dict[str, List[Tuple[str, Optional[float], str]]] = {}
    for method, score, reason in ordered_methods:
        method_class = method_data.get(method, {}).get('class') or method.rsplit('.', 1)[0]
        methods_by_class.setdefault(method_class, []).append((method, score, reason))

    selected: List[str] = []
    seen: Set[str] = set()
    for cls in top_classes:
        entries = methods_by_class.get(cls, [])
        count = 0
        for method, _, _ in entries:
            if method in seen:
                continue
            selected.append(method)
            seen.add(method)
            count += 1
            if count >= methods_per_class:
                break

    if len(selected) < max_hypotheses * methods_per_class:
        for method, _, _ in ordered_methods:
            if method in seen:
                continue
            selected.append(method)
            seen.add(method)
            if len(selected) >= max_hypotheses * methods_per_class:
                break

    return selected


def score_classes(
    gpt: GPTClient,
    hypotheses: List[Dict[str, Any]],
    candidate_classes: List[str],
    facts: Dict[str, Any],
    components: Dict[str, Any],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    class_methods_summary = facts.get('class_methods') or {}
    covered_methods = facts.get('covered_methods') or []
    results: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for hyp in hypotheses:
        for cls in candidate_classes:
            covered = [m for m in covered_methods if isinstance(m, str) and m.startswith(f"{cls}.")]
            covered_text = summarize_list([m.split('.')[-1] for m in covered]) if covered else ""
            methods = class_methods_summary.get(cls, [])
            methods_text = summarize_list([str(m) for m in methods], limit=12)
            if methods_text:
                covered_desc = f"Covered methods: {methods_text}"
            elif covered_text:
                covered_desc = f"Covered methods: {covered_text}"
            else:
                covered_desc = "Covered methods unavailable in facts."

            lines = [
                "[REL=class_score]",
                "You are evaluating whether the candidate class contributes to the failing test described below.",
                f"Failure context:\n- Test code snippet: {components['code']}\n- Stack trace excerpt: {components['stack_trace']}\n- Test output snippet: {components['output']}",
                f"Hypothesis {hyp['id']}: {hyp['text']}",
                f"Class under review: {cls}",
                covered_desc,
                "Return only a number between 0 and 1 representing the likelihood this class causes the failure.",
            ]
            score_text = gpt.call("\n".join(lines), f"class_score {cls} {hyp['id']}")
            score = coerce_float(score_text)

            lines_expl = [
                "[REL=class_explanation]",
                f"Given the failure context (test code: {components['code']}; stack trace: {components['stack_trace']}; output: {components['output']}), explain in <=3 sentences how class '{cls}' and its methods ({methods_text or 'unknown'}) support or contradict hypothesis {hyp['id']}: {hyp['text']}. Focus on concrete behaviors or data flows."
            ]
            explanation = gpt.call("\n".join(lines_expl), f"class_explanation {cls} {hyp['id']}")

            results[(cls, hyp['id'])] = {
                "score": score,
                "explanation": explanation,
            }

    return results


def score_methods(
    gpt: GPTClient,
    hypotheses: List[Dict[str, Any]],
    methods: List[str],
    method_data: Dict[str, Dict[str, Any]],
    facts: Dict[str, Any],
    components: Dict[str, Any],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    results: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for hyp in hypotheses:
        for method in methods:
            data = method_data.get(method, {})
            source = data.get('source')
            method_doc = data.get('method_doc')

            context_lines = [
                "[REL=method_score]",
                "You are evaluating whether this method is responsible for the failing test.",
                "Failure context:",
                f"- Test code snippet: {components['code']}",
                f"- Stack trace excerpt: {components['stack_trace']}",
                f"- Test output snippet: {components['output']}",
                f"Hypothesis {hyp['id']}: {hyp['text']}",
                f"Method under review: {method}",
            ]

            # Add enhanced method documentation if available
            if method_doc:
                context_lines.append(f"Method summary (enhanced): {truncate_text(str(method_doc), 400)}")

            # Add source code if available
            if source:
                context_lines.append(f"Source snippet: {truncate_text(source, 600)}")
            else:
                context_lines.append("Source snippet unavailable.")

            context_lines.append("Return only a number between 0 and 1 estimating the likelihood this method causes the failure.")
            prompt = "\n".join(context_lines)
            score = coerce_float(gpt.call(prompt, f"method_score {method} {hyp['id']}"))

            # Build explanation context
            expl_context = [
                "[REL=method_explanation]",
                f"Given the failure context (test code: {components['code']}; stack trace: {components['stack_trace']}; output: {components['output']}),",
                f"explain in <=3 sentences how method '{method}' supports or contradicts hypothesis {hyp['id']}: {hyp['text']}.",
            ]

            if method_doc:
                expl_context.append(f"Method summary: {truncate_text(str(method_doc), 300)}")

            if source:
                expl_context.append(f"Source: {truncate_text(source, 200)}")

            expl_context.append("Cite concrete argument values or call flows.")
            expl_prompt = "\n".join(expl_context)
            explanation = gpt.call(expl_prompt, f"method_explanation {method} {hyp['id']}")

            results[(method, hyp['id'])] = {
                "score": score,
                "explanation": explanation,
            }

    return results


def tie_break_methods(
    gpt: GPTClient,
    methods: List[str],
    method_stats: Dict[str, Dict[str, Any]],
    hypotheses: List[Dict[str, Any]],
    method_details: Dict[Tuple[str, str], Dict[str, Any]],
    components: Dict[str, Any],
) -> Dict[str, float]:
    if len(methods) <= 1:
        return {methods[0]: 1.0} if methods else {}

    lines = [
        "🔀 TIE-BREAKING RANKING REQUIRED",
        "=" * 50,
        f"Multiple methods share the highest suspicion score of {method_stats.get(methods[0], {}).get('max_score', 'unknown')}.",
        f"Test suite: {components['test_suite']}",
        f"Failed test(s): {components['failed_tests']}",
        f"Assertion details: {components.get('assertion', '')}",
        "",
        "CONTEXT:",
        components['summary'],
        "",
        "METHODS TO RANK:",
        "=" * 20,
    ]
    
    for i, method in enumerate(methods, 1):
        stats = method_stats.get(method, {})
        lines.append(f"{i}. {method}")
        lines.append(f"   Max score: {stats.get('max_score')}")
        for hyp in hypotheses:
            detail = method_details.get((method, hyp['id']), {})
            if detail:
                lines.append(f"   {hyp['id']} (conf: {hyp.get('confidence', 'n/a')}): {detail.get('score', 'n/a')}")
                expl = truncate_text(detail.get('explanation', ''), 150)
                if expl:
                    lines.append(f"      {expl}")
        lines.append("")
    
    lines.extend([
        "🎯 RANKING INSTRUCTIONS:",
        "=" * 25,
        "You MUST rank these methods from MOST LIKELY to LEAST LIKELY to be the buggy method.",
        "Each method must have a DIFFERENT tie_break_score - NO TIES ALLOWED!",
        "",
        "Score guidelines:",
        "- Most suspicious method: 0.90-1.00",
        "- Second most suspicious: 0.70-0.89", 
        "- Third most suspicious: 0.50-0.69",
        "- Least suspicious: 0.10-0.49",
        "",
        "Consider:",
        "- Method complexity and likelihood of bugs",
        "- Parameter differences (more parameters = more complexity)",
        "- Method purpose (constructors vs business logic)",
        "- Error handling patterns",
        "",
        "Return ONLY a JSON array with this exact format:",
        "[",
        '  {"method": "exact.method.name", "tie_break_score": 0.95},',
        '  {"method": "exact.method.name", "tie_break_score": 0.82},',
        '  {"method": "exact.method.name", "tie_break_score": 0.65},',
        '  {"method": "exact.method.name", "tie_break_score": 0.43}',
        "]",
        "",
        "⚠️  CRITICAL: All tie_break_score values must be different!"
    ])

    prompt = "[REL=method_tie_break]\n" + "\n".join(lines)
    raw = gpt.call(prompt, "method_tie_break")
    print(f"  🔍 Raw tie-breaking response: {raw}")
    scores, _ = parse_ranked_methods(raw, methods)
    print(f"  📊 Parsed tie-breaking scores: {scores}")
    return scores


def write_csv(
    output_path: Path,
    rows: List[Tuple[str, float]],
    method_stats: Dict[str, Dict[str, Any]],
    hypothesis_map: Dict[str, Dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "method", 
            "score", 
            "hypothesis_id", 
            "hypothesis_confidence", 
            "hypothesis_text", 
            "class_match",
            "hypothesis_explanation"
        ])
        for method, score in rows[:10]:
            stats = method_stats.get(method, {})
            hyp_id = stats.get("best_hypothesis")
            hyp_conf = stats.get("best_hypothesis_confidence")
            hyp_text = hypothesis_map.get(hyp_id, {}).get("text") if hyp_id else ""
            class_match = stats.get("best_class_match") or ""
            hyp_explanation = stats.get("best_hypothesis_explanation") or ""
            writer.writerow([
                method,
                f"{score:.6f}",
                hyp_id or "",
                f"{hyp_conf:.6f}" if isinstance(hyp_conf, (int, float)) else "",
                hyp_text,
                class_match,
                hyp_explanation,
            ])

def write_token_usage_csv(output_path: Path, token_usage: Dict[str, int]) -> None:
    """Write token usage to a separate CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerow(["total_api_calls", token_usage.get("total_calls", 0)])
        writer.writerow(["total_tokens", token_usage.get("total_tokens", 0)])
        writer.writerow(["prompt_tokens", token_usage.get("prompt_tokens", 0)])
        writer.writerow(["completion_tokens", token_usage.get("completion_tokens", 0)])


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run GPT-only neuro-symbolic FL pipeline")
    parser.add_argument("case_id", help="Case identifier, e.g. Chart-20")
    parser.add_argument("--facts-dir", type=Path, default=Path("soapfl_facts"))
    parser.add_argument("--output-dir", type=Path, default=None, help="Optional output directory (defaults to <case>_parallel_case)")
    parser.add_argument("--max-hypotheses", type=int, default=MAX_HYPOTHESES)
    parser.add_argument("--classes-per-hypothesis", type=int, default=CLASSES_PER_HYPOTHESIS)
    parser.add_argument("--methods-per-class", type=int, default=METHODS_PER_CLASS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY environment variable is required")
    # API key is automatically used by openai.OpenAI() from environment

    case_dir = args.output_dir or Path(f"{args.case_id}_parallel_case")
    case_dir.mkdir(parents=True, exist_ok=True)

    facts_path = args.facts_dir / args.case_id.split('-')[0] / args.case_id.split('-')[1] / "facts.json"
    if not facts_path.exists():
        raise SystemExit(f"Facts file not found: {facts_path}")

    facts = load_facts_from_json(str(facts_path))
    components = build_failure_components(facts)

    gpt = GPTClient(model=args.model)

    print(f"=== GPT-only pipeline for {args.case_id} ===")
    hypotheses = generate_hypotheses(gpt, components, args.max_hypotheses)
    if not hypotheses:
        raise SystemExit("Failed to generate hypotheses")
    print("Hypotheses:")
    for hyp in hypotheses:
        print(f"  {hyp['id']} (confidence {hyp.get('confidence', 0.0):.3f}): {hyp['text']}")

    covered_classes, excluded = filter_covered_classes(facts)
    if excluded:
        print(f"Ignoring {len(excluded)} covered classes without method coverage")
    if not covered_classes:
        raise SystemExit("No covered classes with methods found")

    ordered_classes = pre_rank_classes(
        gpt,
        covered_classes,
        facts,
        components,
        args.classes_per_hypothesis,
    )
    candidate_classes = [cls for cls, _, _ in ordered_classes[: args.classes_per_hypothesis]]
    print("Candidate classes:")
    for cls, score, reason in ordered_classes:
        score_note = f"{score:.3f}" if score is not None else "n/a"
        print(f"  {cls}: {score_note} {reason if reason else ''}")

    method_pool = gather_methods_for_classes(candidate_classes, facts, facts.get('covered_methods') or [])
    print(f"Collected {len(method_pool)} methods across candidate classes")

    large_classes = {
        cls: methods
        for cls, methods in facts.get('class_methods', {}).items()
        if cls in candidate_classes and len(methods) > LARGE_CLASS_THRESHOLD
    }
    if large_classes:
        prefilter_results = prefilter_large_classes(gpt, large_classes, components)
        allowed_methods = set(method_pool)
        for cls, selected in prefilter_results.items():
            full_names = [m if m.startswith(f"{cls}.") else f"{cls}.{m}" for m in selected]
            original = [m for m in method_pool if m.startswith(f"{cls}.")]
            allowed_methods.difference_update(original)
            allowed_methods.update(full_names)
        method_pool = [m for m in method_pool if m in allowed_methods]
        print(f"Method pool reduced to {len(method_pool)} after large-class prefilter")

    ordered_methods, method_data = pre_rank_methods(gpt, method_pool, facts, components)
    candidate_methods = select_candidate_methods(
        ordered_methods,
        method_data,
        candidate_classes,
        args.methods_per_class,
        args.max_hypotheses,
    )
    print(f"Selected {len(candidate_methods)} candidate methods")

    class_scores = score_classes(gpt, hypotheses, candidate_classes, facts, components)
    method_scores = score_methods(gpt, hypotheses, candidate_methods, method_data, facts, components)

    method_stats: Dict[str, Dict[str, Any]] = {}
    for method in candidate_methods:
        best_hyp_id = None
        best_hyp_score = -1.0
        best_hyp_expl = ""
        method_class = method_data.get(method, {}).get('class')
        best_class_match: Optional[str] = None
        best_class_score: Optional[float] = None

        for hyp in hypotheses:
            detail = method_scores.get((method, hyp['id']), {})
            score = detail.get('score')
            if score is None:
                continue
            if score > best_hyp_score:
                best_hyp_score = score
                best_hyp_id = hyp['id']
                best_hyp_expl = detail.get('explanation', '')
            if method_class:
                class_score = class_scores.get((method_class, hyp['id']), {}).get('score')
                if class_score is not None:
                    if best_class_score is None or class_score > best_class_score:
                        best_class_score = class_score
                        best_class_match = f"{method_class} (H{hyp['id']})"

        max_score = best_hyp_score if best_hyp_score > 0 else 0.0
        method_stats[method] = {
            "max_score": max_score,
            "best_hypothesis": best_hyp_id,
            "best_hypothesis_explanation": best_hyp_expl,
            "best_hypothesis_confidence": next(
                (hyp.get('confidence', 0.0) for hyp in hypotheses if hyp['id'] == best_hyp_id),
                None,
            ),
            "best_class_match": best_class_match,
            "best_class_score": best_class_score,
        }

    suspicious: List[Tuple[str, float]] = []
    for method, stats in method_stats.items():
        suspicious.append((method, stats['max_score']))
    suspicious.sort(key=lambda item: item[1], reverse=True)

    if suspicious:
        top_score = suspicious[0][1]
        tied = [method for method, score in suspicious if math.isclose(score, top_score, rel_tol=1e-6, abs_tol=1e-9)]
        if len(tied) > 1:
            print(f"  🔀 Tie-breaking {len(tied)} methods with score {top_score:.6f}")
            method_details = {
                (method, hyp['id']): method_scores.get((method, hyp['id']), {})
                for method in tied
                for hyp in hypotheses
            }
            tie_scores = tie_break_methods(gpt, tied, method_stats, hypotheses, method_details, components)
            print(f"  🎯 Tie-breaking scores: {tie_scores}")
            for idx, (method, score) in enumerate(suspicious):
                if method in tie_scores:
                    # Add tie-breaking score as a more significant adjustment
                    tie_adjustment = tie_scores[method] * 1e-2  # Increased to 1e-2 for visibility
                    new_score = score + tie_adjustment
                    suspicious[idx] = (method, new_score)
                    print(f"    {method}: {score:.6f} + {tie_adjustment:.6f} = {new_score:.6f}")
            suspicious.sort(key=lambda item: item[1], reverse=True)
            print(f"  ✅ Final ranking after tie-breaking:")
            for i, (method, score) in enumerate(suspicious[:len(tied)], 1):
                print(f"    {i}. {method}: {score:.6f}")

    hypothesis_map = {hyp['id']: hyp for hyp in hypotheses}
    summary_lines = [f"# GPT-only Results for {args.case_id}\n"]
    summary_lines.append("## Top Suspicious Methods\n")
    print("\nTop suspicious methods:")
    for idx, (method, score) in enumerate(suspicious[:10], 1):
        stats = method_stats.get(method, {})
        hyp_id = stats.get('best_hypothesis')
        hyp = hypothesis_map.get(hyp_id, {}) if hyp_id else {}
        hyp_text = hyp.get('text', '(unknown hypothesis)') if hyp else '(no hypothesis)'
        hyp_conf = stats.get('best_hypothesis_confidence')
        class_match = stats.get('best_class_match')
        expl = truncate_text(stats.get('best_hypothesis_explanation', ''), 240)
        hyp_note = f"best hypothesis {hyp_id}: {hyp_text}" if hyp_id else "no supporting hypothesis"
        class_note = f"; supporting class {class_match}" if class_match else ""
        conf_note = f" (confidence {hyp_conf:.3f})" if isinstance(hyp_conf, (int, float)) else ""
        print(f"  {idx}. {method}: {score:.3f} — {hyp_note}{conf_note}{class_note}")
        if expl:
            print(f"      explanation: {expl}")
        summary_lines.append(
            f"{idx}. **{method}** — score {score:.3f}. {hyp_note}{conf_note}{class_note}."
        )
        if expl:
            summary_lines.append(f"    Explanation: {expl}\n")

    # Display token usage summary
    token_summary = gpt.get_token_summary()
    print(f"\n📊 Token Usage Summary:")
    print(f"  Total API calls: {token_summary['total_calls']}")
    print(f"  Total tokens: {token_summary['total_tokens']:,}")
    print(f"  Prompt tokens: {token_summary['prompt_tokens']:,}")
    print(f"  Completion tokens: {token_summary['completion_tokens']:,}")
    
    # Add token usage to summary
    summary_lines.append(f"\n## Token Usage\n")
    summary_lines.append(f"- **Total API calls**: {token_summary['total_calls']}")
    summary_lines.append(f"- **Total tokens**: {token_summary['total_tokens']:,}")
    summary_lines.append(f"- **Prompt tokens**: {token_summary['prompt_tokens']:,}")
    summary_lines.append(f"- **Completion tokens**: {token_summary['completion_tokens']:,}")

    answer_path = case_dir / f"{args.case_id}_parallel_answer.csv"
    write_csv(answer_path, suspicious, method_stats, hypothesis_map)
    
    token_path = case_dir / f"{args.case_id}_token_usage.csv"
    write_token_usage_csv(token_path, token_summary)
    
    summary_path = case_dir / f"{args.case_id}_parallel_summary.md"
    summary_path.write_text("\n".join(summary_lines) + "\n")
    print(f"Results written to {answer_path}")
    print(f"Token usage written to {token_path}")
    print(f"Summary written to {summary_path}")


if __name__ == "__main__":
    main()
