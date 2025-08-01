import abc
import json
import os
import re
import time
from importlib import resources
from typing import Any, Dict, List, Optional, cast

import fitz
import requests
import toml
from rich import print

from .configs import Config
from .utils.checker import Checker
from .utils.error_handler import api_calling_error_exponential_backoff
from .utils.llm import create_client, get_response_from_llm

# Load config
config_path = os.path.join(os.path.dirname(__file__), "config.toml")
config = toml.load(config_path) if os.path.exists(config_path) else {"core": {}}


class BaseTool(abc.ABC):
    def __init__(self, cost_tracker: Optional[Checker] = None) -> None:
        self.cost_tracker = cost_tracker or Checker()
        self.github_token = config["core"].get("github_token", None)

    @abc.abstractmethod
    def run(self, query: str) -> Dict[str, Dict[str, str]]:
        pass


class CodeSearchTool(BaseTool):
    def __init__(self) -> None:
        super().__init__()

    def run(
        self, query: str, search_type: str = "repositories"
    ) -> Dict[str, Dict[str, str]]:
        print(f"[github API calling] Searching for code with query: {query}")
        results = {}

        try:
            idea = json.loads(query)
            if isinstance(idea, dict) and any(
                k in idea for k in ["Title", "Experiment"]
            ):
                query = self.format_github_repo_query(idea)
                print(f"[github API calling] Formatted query from idea: {query}")
        except (json.JSONDecodeError, TypeError):
            pass

        repos = self._search_github(query=query, search_type=search_type)

        if repos:
            for i, repo in enumerate(repos):
                results[str(i)] = {
                    "title": repo["name"],
                    "source": repo["url"],
                    "info": f"Stars: {repo['stars']}",
                }

        self.cost_tracker.report()
        return results

    def format_github_repo_query(
        self, idea: Dict[str, Any], max_terms: int = 6, max_query_length: int = 250
    ) -> str:
        import re

        import spacy

        title = idea.get("Title", "")
        experiment = idea.get("Experiment", "")
        combined_text = f"{title}. {experiment}"

        nlp = spacy.load("en_core_web_sm")
        doc = nlp(combined_text)
        candidates = set()

        # Extract short noun phrases
        for chunk in doc.noun_chunks:
            phrase = chunk.text.strip().lower()
            if 1 <= len(phrase.split()) <= 4:
                candidates.add(phrase)

        # Add important standalone nouns and proper nouns
        for token in doc:
            if token.pos_ in {"NOUN", "PROPN"} and len(token.text) > 2:
                candidates.add(token.text.lower())

        # Clean and deduplicate
        seen = set()
        keywords = []
        for kw in candidates:
            cleaned = re.sub(r"[^\w\s]", "", kw)
            if cleaned not in seen:
                seen.add(cleaned)
                keywords.append(cleaned)
            if len(keywords) >= max_terms:
                break

        # Build query string
        quoted_keywords = [f'"{kw}"' if " " in kw else kw for kw in keywords]
        base_query = " ".join(quoted_keywords)
        suffix = " in:file language:python"
        full_query = f"{base_query} {suffix}"

        # Truncate if needed
        if len(full_query) > max_query_length:
            full_query = f"{' '.join(quoted_keywords[:max_terms//2])} {suffix}"

        return full_query

    def _search_github(
        self, query: str, search_type: str, result_limit: int = 10
    ) -> Optional[List[Dict[str, Any]]]:
        if search_type not in ["repositories", "code"]:
            raise ValueError("search_type must be either 'repositories' or 'code'.")

        url = f"https://api.github.com/search/{search_type}"
        headers = (
            {"Authorization": f"token {self.github_token}"} if self.github_token else {}
        )

        params = {
            "q": query,
            "sort": "stars" if search_type == "repositories" else "indexed",
            "order": "desc",
            "per_page": result_limit,
        }

        response = requests.get(url, headers=headers, params=params)
        print(
            f"GitHub {search_type.capitalize()} Response Status Code: {response.status_code}"
        )
        response.raise_for_status()

        results = response.json()
        if "items" not in results:
            return None

        return (
            self._extract_github_repo_info(results["items"])
            if search_type == "repositories"
            else self._extract_github_code_info(results["items"])
        )

    @staticmethod
    def _extract_github_repo_info(repos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "name": repo["name"],
                "owner": repo["owner"]["login"],
                "stars": repo["stargazers_count"],
                "forks": repo["forks_count"],
                "url": repo["html_url"],
                "description": repo["description"] or "No description provided.",
            }
            for repo in repos
        ]

    @staticmethod
    def _extract_github_code_info(
        code_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return [
            {
                "file_name": item["name"],
                "repository": item["repository"]["full_name"],
                "url": item["html_url"],
            }
            for item in code_results
        ]


class PaperSearchTool(BaseTool):
    def __init__(self, s2_api_key: Optional[str] = None) -> None:
        super().__init__()
        self.s2_api_key = (
            s2_api_key
            or os.environ.get("S2_API_KEY")
            or config["core"].get("s2_api_key")
        )

        # Set default engine if not configured
        self.engine = config["core"].get("engine", "semanticscholar")

    def run(self, query: str) -> Dict[str, Dict[str, str]]:
        results = {}
        papers = self.search_for_papers(query)

        if papers:
            for i, paper in enumerate(papers):
                paper_id = paper.get("paperId", None)
                bibtex = self.fetch_bibtex(paper_id) if paper_id else "N/A"

                if not bibtex or bibtex == "N/A":
                    continue

                results[paper["title"]] = {"title": paper["title"], "bibtex": bibtex}

        self.cost_tracker.report()
        return results

    def search_for_papers(
        self, query: str, result_limit: int = 3
    ) -> Optional[List[Dict[str, Any]]]:
        if not query:
            return None

        if self.engine == "semanticscholar":
            print(
                f"(semantic scholar API calling) Searching for papers with query: {query}"
            )
            return self._search_semanticscholar(query, result_limit)
        elif self.engine == "openalex":
            print(f"(openalex API calling) Searching for papers with query: {query}")
            return self._search_openalex(query, result_limit)
        else:
            raise NotImplementedError(f"{self.engine=} not supported!")

    @api_calling_error_exponential_backoff(retries=5, base_wait_time=2)
    def _search_semanticscholar(
        self, query: str, result_limit: int
    ) -> Optional[List[Dict[str, Any]]]:
        params: Dict[str, str | int] = {
            "query": query,
            "limit": result_limit,
            "fields": "title,authors,venue,year,abstract,citationStyles,citationCount,paperId",
        }

        headers = {"X-API-KEY": self.s2_api_key} if self.s2_api_key else {}
        rsp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers=headers,
            params=params,
        )
        rsp.raise_for_status()

        results = rsp.json()
        if not results.get("total"):
            return None

        time.sleep(1.0)
        return cast(Optional[List[Dict[str, Any]]], results.get("data"))

    def _search_openalex(
        self, query: str, result_limit: int
    ) -> Optional[List[Dict[str, Any]]]:
        import pyalex
        from pyalex import Works

        mail = os.environ.get("OPENALEX_MAIL_ADDRESS")
        if mail:
            pyalex.config.email = mail
        else:
            print("[WARNING] Please set OPENALEX_MAIL_ADDRESS for better API access")

        works = Works().search(query).get(per_page=result_limit)
        if not works:
            return None

        return [self._extract_work_info(work) for work in works]

    @api_calling_error_exponential_backoff(retries=5, base_wait_time=2)
    def fetch_bibtex(self, paper_id: str) -> Any:
        headers = {"X-API-KEY": self.s2_api_key} if self.s2_api_key else {}
        rsp = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}",
            headers=headers,
            params={"fields": "citationStyles"},
        )
        rsp.raise_for_status()
        citation_styles = rsp.json().get("citationStyles", {})
        return citation_styles.get("bibtex", "N/A")

    @staticmethod
    def _extract_work_info(
        work: Dict[str, Any], max_abstract_length: int = 1000
    ) -> Dict[str, str]:
        venue = next(
            (
                loc["source"]["display_name"]
                for loc in work["locations"]
                if loc["source"]
            ),
            "Unknown",
        )

        authors_list = [
            author["author"]["display_name"] for author in work["authorships"]
        ]
        authors = (
            " and ".join(authors_list)
            if len(authors_list) < 20
            else f"{authors_list[0]} et al."
        )

        abstract = work.get("abstract", "")
        if len(abstract) > max_abstract_length:
            print(f"[WARNING] {work['title']}: Abstract is too long, truncating.")
            abstract = abstract[:max_abstract_length]

        return {
            "title": work["title"],
            "authors": authors,
            "venue": venue,
            "year": work.get("publication_year", "Unknown"),
            "abstract": abstract,
            "citationCount": work.get("cited_by_count", 0),
        }


class DrawerTool(BaseTool):
    def __init__(
        self,
        model: Any,
        prompt_template_dir: Optional[str] = None,
        temperature: float = 0.75,
    ):
        super().__init__()
        self.client, self.model = create_client(model)
        self.temperature = temperature

        # Load prompt templates using Config
        self.config = Config(prompt_template_dir)
        self.prompts = self.config.prompt_template.drawer_prompt

        def escape_curly_braces(text: str) -> str:
            return re.sub(r"({|})", r"{{\1}}", text)

        def extract_pdf_text_from_resource(package: str, filename: str) -> str:
            with resources.files(package).joinpath(filename).open("rb") as f:
                doc = fitz.open(stream=f.read(), filetype="pdf")
                extracted = [page.get_text().strip() for page in doc]
                return "\n\n".join(extracted)

        method_sample_raw = extract_pdf_text_from_resource(
            "tiny_scientist.fewshot_sample", "framework.pdf"
        )
        result_sample_raw = extract_pdf_text_from_resource(
            "tiny_scientist.fewshot_sample", "result.pdf"
        )

        method_sample = escape_curly_braces(method_sample_raw)
        result_sample = escape_curly_braces(result_sample_raw)

        self.system_prompts = self.prompts.diagram_system_prompt.format(
            method_sample=method_sample,
            result_sample=result_sample,
        )

        self.dir_path = os.path.dirname(os.path.realpath(__file__))

    def run(self, query: str) -> Dict[str, Dict[str, str]]:
        try:
            query_dict = json.loads(query)
            section_name = query_dict.get("section_name")
            section_content = query_dict.get("section_content")
        except (json.JSONDecodeError, TypeError, AttributeError):
            raise ValueError(
                "Expected query to be a JSON string with 'section_name' and 'section_content'."
            )

        diagram = self.draw_diagram(
            section_name=section_name, section_content=section_content
        )

        results = {}
        if diagram:
            results["diagram"] = {
                "summary": diagram.get("summary", ""),
                "svg": diagram.get("svg", ""),
            }
        self.cost_tracker.report()
        return results

    def draw_diagram(
        self,
        section_name: str,
        section_content: str,
        msg_history: Optional[List[Dict[str, Any]]] = None,
        return_msg_history: bool = False,
    ) -> Any:
        # Use default system prompt if none provided
        section_prompt = self._get_section_prompts(section_name, section_content)

        diagram, updated_msg_history = self._generate_diagram(
            section_prompt, self.system_prompts, msg_history
        )

        return (diagram, updated_msg_history) if return_msg_history else diagram

    def _get_section_prompts(self, section_name: str, section_text: str) -> str:
        section_prompt = self.prompts.section_prompt[section_name].format(
            section_text=section_text
        )

        return section_prompt

    @api_calling_error_exponential_backoff(retries=5, base_wait_time=2)
    def _generate_diagram(
        self,
        section_prompt: str,
        drawer_system_prompt: str,
        msg_history: Optional[List[Dict[str, Any]]],
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        # Ensure msg_history is a list
        msg_history = msg_history or []

        # Generate diagram
        llm_response, msg_history = get_response_from_llm(
            section_prompt,
            model=self.model,
            client=self.client,
            system_message=drawer_system_prompt,
            msg_history=msg_history,
            temperature=self.temperature,
            cost_tracker=self.cost_tracker,
            task_name="generate_diagram",
        )

        diagram = self._extract_diagram(llm_response)
        return diagram, msg_history

    def _extract_diagram(self, response: str) -> Dict[str, Any]:
        result = {"summary": "", "svg": "", "full_response": response}

        try:
            parsed = json.loads(response)
            summary = parsed["summary"]
            svg = parsed["svg"]
        except json.JSONDecodeError:
            svg_match = re.search(r"<svg.*?</svg>", response, re.DOTALL)
            svg = svg_match.group(0) if svg_match else ""
            summary = (
                re.sub(r"<svg.*?</svg>", "", response, flags=re.DOTALL)
                .strip()
                .split("\n")[0]
            )

        if "<svg" in svg and "</svg>" in svg:
            result["summary"] = summary
            result["svg"] = self._clean_svg(svg)
        else:
            print("[ERROR] SVG missing or too short.")
        return result

    def _clean_svg(self, svg: str) -> str:
        # Strip any outer code block delimiters
        svg = svg.strip()
        svg = re.sub(r"^```(?:svg)?", "", svg)
        svg = re.sub(r"```$", "", svg)

        # Replace problematic ampersands
        svg = svg.replace("&", "&amp;")

        # Ensure no double XML declarations
        svg = re.sub(r"<\?xml.*?\?>", "", svg, count=1)

        # Remove extra whitespace lines
        svg = "\n".join([line for line in svg.splitlines() if line.strip()])

        return svg.strip()
