#!/usr/bin/env python3
"""
Copyright (c) 2022-2023, Ross Smith II

SPDX-License-Identifier: MIT
"""
# pylint: disable=W0703 # Catching too general exception Exception (broad-except)
# C0209: Formatting a regular string which could be a f-string (consider-using-f-string)

import glob
import json
import logging
import os
import re
import shlex
import shutil
import sys
import time
from enum import IntEnum
from subprocess import PIPE, Popen  # nosec
from typing import Any

import pdfkit  # type: ignore
from pypdf import PdfMerger, PdfReader  # type: ignore

# from PyPDF2 import PdfMerger, PdfReader  # type: ignore
from selenium import webdriver  # type: ignore
from selenium.webdriver.common.by import By  # type: ignore

DEFAULT_TOC_CODE = "CIV"

# editorconfig-checker-disable
# https://leginfo.legislature.ca.gov/faces/codes_displayexpandedbranch.xhtml?tocCode=CIV&division=1.&title=&part=1.&chapter=&article=

# pylint: disable=C0301 # Line too long (157/132) (line-too-long)
# E501 # Line too long (143/132) (line-too-long)
URL_MASK = "https://leginfo.legislature.ca.gov/faces/codes_displayexpandedbranch.xhtml?tocCode=%s&division=%s&title=&part=%s&chapter=&article="  # noqa: E501
# editorconfig-checker-enable

SECTION_NAMES = ["DIVISION", "PART", "TITLE", "CHAPTER", "ARTICLE"]

CONTAINS_TEXT_XPATH = '//*[contains(text(),"%s ")]'
MANYLAWSECTIONS_XPATH = '//*[@id="manylawsections"]'
EXPANDEDBRANCHCODESID_XPATH = '//*[@id="expandedbranchcodesid"]'
EXPANDEDBRANCHCODESIDA_XPATH = '//*[@id="expandedbranchcodesid"]/*/a[@href]'

ERR_DIR = "err"
HTML_DIR = "html"
PDF_DIR = "pdf"


class Retval(IntEnum):
    """doc me"""

    OK = 0
    OK_FILE_ALREADY_EXISTS = -1
    ERROR = 1


# pylint: disable=R0902 # Too many instance attributes (14/7) (too-many-instance-attributes)
# pylint: disable=R0904 # Too many public methods (23/20) (too-many-public-methods)
class LawScraper:
    """doc me"""

    def __init__(self) -> None:
        """class initializer"""
        super().__init__()
        # pylint: disable=C0103 # Attribute name "ci" doesn't conform to snake_case naming style (invalid-name)
        self.ci = os.getenv("CI") is not None
        self.division: str = ""
        self.downloads_dir = os.path.expanduser("~/Downloads")
        self.driver: Any = None
        self.has_tidy = shutil.which("tidy")
        self.log_level = logging.DEBUG
        self.merger: Any = None
        self.next_page = 0
        self.output_pdf = ""
        self.parents: dict[str, Any] = {}
        self.part: str = ""
        self.pdfs: list[str] = []
        self.toc_code = DEFAULT_TOC_CODE
        self.url = ""

        logging.root.setLevel(self.log_level)
        requests_log = logging.getLogger("urllib3")
        requests_log.setLevel(logging.ERROR)

    def init_driver(self) -> None:
        """doc me"""

        app_state = {
            "recentDestinations": [{"id": "Save as PDF", "origin": "local", "account": ""}],
            "selectedDestinationId": "Save as PDF",
            "version": 2,
        }

        prefs = {
            "download.default_directory": self.downloads_dir,
            "profile.default_content_settings.popups": 0,
            "printing.print_preview_sticky_settings.appState": json.dumps(app_state),  # noqa
        }

        options = webdriver.ChromeOptions()  # type: ignore
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--kiosk-printing")  # type: ignore
        options.add_argument("--log-level=3")  # type: ignore
        if self.ci:
            # see https://github.com/rasa/law-scraper/runs/4740875776?check_suite_focus=true#step:8:8  # noqa
            # see https://stackoverflow.com/a/47887610/1432614
            options.add_argument("--no-sandbox")  # type: ignore
            options.add_argument("--headless")  # type: ignore
        self.driver = webdriver.Chrome(options=options)  # type: ignore

        webdriver.remote.remote_connection.LOGGER.setLevel(logging.CRITICAL)  # type: ignore # github only

    def append_pdf(self, url: str, title: str) -> Retval:
        """doc me"""
        prefix = self.get_prefix(title)
        logging.debug("prefix=%s title=%s", prefix, title)

        parent = None
        if prefix == "":
            prefix = "PART"
        if prefix == "PART":
            self.set_output_name(url, title)
        elif prefix == "TITLE":
            parent = self.parents["PART"]
        elif prefix == "CHAPTER":
            if "TITLE" not in self.parents:
                parent = self.parents["PART"]
            else:
                parent = self.parents["TITLE"]
        elif prefix == "ARTICLE":
            parent = self.parents["CHAPTER"]
        else:
            logging.error("Unknown prefix: %s", prefix)

        if os.path.exists(self.output_pdf):
            logging.info("File already exists: %s", self.output_pdf)
            return Retval.OK_FILE_ALREADY_EXISTS

        pdf = self.get_pdf(url, title)
        if not pdf:
            return Retval.ERROR
        self.pdfs.append(pdf)
        try:
            logging.debug("Merging %s", pdf)
            # see https://stackoverflow.com/a/52817391/1432614
            self.merger.append(pdf, import_outline=False)
        except Exception as exc:
            logging.error("Merging %s failed: %s", pdf, str(exc))
            return Retval.ERROR

        bookmark = self.merger.add_outline_item(title, self.next_page, parent)
        self.parents[prefix] = bookmark
        self.next_page += self.get_num_pages(pdf)
        return Retval.OK

    def get(self, url: str) -> bool:
        """doc me"""
        if self.url == url:
            return True
        self.url = url
        logging.debug("Retrieving %s", url)
        self.driver.get(url)
        return True

    @staticmethod
    def get_num_pages(pdf: str) -> Any:
        """doc me"""
        # Using with causes: AttributeError: __enter__
        # _pylint: disable=R1732 # Consider using 'with' for resource-allocating operations (consider-using-with)
        return len(PdfReader(open(pdf, "rb"), strict=False).pages)

    def get_pdf(self, url: str, title: str) -> str:
        """doc me"""
        self.get(url)

        filename = self.normalize(title)
        numbers = self.get_section_numbers(url)
        prefix = ""
        for section in reversed(SECTION_NAMES):
            if numbers[section]:
                if re.match(section, filename, re.I) is None:
                    prefix = numbers[section] + "_" + prefix

        filename = self.toc_code.lower() + "_" + prefix + filename
        return self.save_pdf(filename)

    @staticmethod
    def get_prefix(title: str) -> str:
        """doc me"""
        parts = re.split(r"\s+", title)
        return parts[0]

    def get_section_numbers(self, url: str) -> dict[str, str]:
        """doc me"""
        rv: dict[str, str] = {}
        for section in SECTION_NAMES:
            lower = section.lower()
            num = self.parse_url_for_id(rf"{lower}=([^&]+)", url)
            if num:
                if num[-1] == ".":
                    num = num[:-1]
            rv[section] = num
        return rv

    def get_section_titles(self, url: str) -> dict[str, str]:
        """doc me"""
        if url:
            self.get(url)

        # See https://stackoverflow.com/a/16608016/1432614
        rv: dict[str, str] = {}
        for section in SECTION_NAMES:
            xpath = CONTAINS_TEXT_XPATH % section
            result = self.xpath(xpath)
            if re.match(section, result) is None:
                result = ""
            rv[section] = result

        return rv

    @staticmethod
    def normalize(path: str) -> str:
        """doc me"""
        path = path.replace(" - ", "-")
        path = re.sub(r"\. ", " ", path)
        # Remove characters that are invalid in windows paths
        path = re.sub(r"[<>:\"/\\|\?\*]+", " ", path)
        path = re.sub(r"\s+", "-", path)
        path = path.lower()
        return path

    @staticmethod
    def parse_url_for_id(regex: str, url: str) -> str:
        """doc me"""
        m = re.search(regex, url)
        if m is None:
            return ""
        return m.group(1)

    def print_pdf(self, pdf: str) -> bool:
        """doc me"""
        if self.ci:
            # Not working yet using headless Chrome
            return False

        logging.debug("Generating %s (using Chrome)", pdf)

        mask = os.path.join(self.downloads_dir, "*.pdf")
        olds = sorted(glob.iglob(mask), key=os.path.getmtime, reverse=True)
        if olds:
            old = olds[0]
        else:
            old = ""

        self.driver.execute_script("window.print();")
        time.sleep(5)
        news = sorted(glob.iglob(mask), key=os.path.getmtime, reverse=True)
        if news:
            new = news[0]
        else:
            new = ""

        if old == new or not new or not os.path.isfile(new):
            return False

        logging.debug("Moving %s to %s", new, pdf)
        try:
            os.rename(new, pdf)
        except Exception as exc:
            logging.error("Cannot move %s to %s: %s", new, pdf, str(exc))  # noqa
            return False
        return True

    @staticmethod
    def run(cmd: str) -> tuple[int, bytes, bytes]:
        """doc me"""
        args = shlex.split(cmd)
        if re.search(r"/\\", args[0]) is None:
            full_path = shutil.which(args[0])
            if full_path:
                args[0] = full_path
        with Popen(args, stdout=PIPE, stderr=PIPE) as process:  # nosec
            (out, err) = process.communicate()
            rv = process.wait()
        return (rv, out, err)

    def save_html(self, prefix: str, html: str) -> bool:
        """doc me"""
        if os.path.exists(html):
            return True

        xpath = MANYLAWSECTIONS_XPATH
        elem = None
        try:
            elem = self.driver.find_element(By.XPATH, xpath)
        except Exception:  # nosec
            pass

        if not elem:
            xpath = EXPANDEDBRANCHCODESID_XPATH
            try:
                elem = self.driver.find_element(By.XPATH, xpath)
            except Exception:
                logging.warning("No element for %s found for %s", xpath, prefix)  # noqa
                html = f"{ERR_DIR}/{prefix}.html"
                logging.debug("Saving %s", html)
                with open(html, "w", encoding="utf-8") as fd:
                    fd.write(self.driver.page_source)
                return False

        inner_html = elem.get_attribute("innerHTML")

        try:
            logging.debug("Saving %s", html)
            with open(html, "w", encoding="utf-8") as fd:
                fd.write(inner_html)
            self.tidy(html)
        except Exception as exc:
            logging.warning("Cannot save %s: %s", html, str(exc))
        return True

    def save_pdf(self, prefix: str) -> str:
        """doc me"""

        pdf = os.path.join(self.downloads_dir, prefix + ".pdf")

        html = f"{HTML_DIR}/{prefix}.html"
        self.save_html(prefix, html)

        if os.path.isfile(pdf):
            logging.info("File already exists: %s", pdf)
            return pdf

        if self.print_pdf(pdf):
            return pdf

        if not os.path.isfile(html):
            logging.warning("No new pdf found in %s", self.downloads_dir)
            return ""

        logging.debug("Generating %s (using Pdfkit)", pdf)
        pdfkit.from_file(html, pdf)
        if not os.path.isfile(pdf):
            logging.warning("Failed to convert %s to %s", html, pdf)
            return ""

        return pdf

    @staticmethod
    def set_log_level(level: str) -> None:
        """doc me"""
        if not level:
            return
        if isinstance(level, str):
            level = level.upper()
        logging.root.setLevel(level)

    def set_output_name(self, url: str, title: str) -> str:
        """doc me"""
        numbers = self.get_section_numbers(url)
        toc = self.toc_code.lower()
        div = numbers["DIVISION"]
        ttl = self.normalize(title)
        self.output_pdf = f"{PDF_DIR}/{toc}_division-{div}_{ttl}.pdf"
        return self.output_pdf

    def tidy(self, html: str) -> bool:
        """doc me"""
        if not self.has_tidy:
            return False

        try:
            logging.debug("Tidying %s", html)
            cmd = f'tidy -config config.tidy --write-back yes "{html}"'
            (rv, _, err) = self.run(cmd)
            if rv != 0:
                logging.warning("Tidy returned error %s processing %s: %s", rv, html, err)
                return False

            with open(html, "r+", encoding="utf-8") as fd:
                html_data = fd.read()
                html_data = re.sub(
                    r"h6.c7 {float: left",
                    "h6.c7 {display: inline; font-size: 100%",
                    html_data,
                )
                html_data = re.sub(r" href=\"[^\"]*\"", "", html_data)
                fd.seek(0)
                fd.write(html_data)
                fd.truncate()
        except Exception as exc:
            logging.warning("Cannot tidy %s: %s", html, str(exc))
            return False

        return True

    @staticmethod
    def usage() -> None:
        """doc me"""
        print(f"Usage: {sys.argv[0]} division part", file=sys.stderr)
        sys.exit(1)

    def version(self) -> None:
        """doc me"""
        (rv, _tag_sha, _) = self.run("git rev-list --tags --max-count=1")
        if rv != 0 or not _tag_sha:
            return
        tag_sha = _tag_sha.decode("utf-8")
        tag_sha = re.sub(r"\s+", "", tag_sha)

        (rv, _tag, _) = self.run(f"git describe --tags {tag_sha}")
        if rv != 0 or not _tag:
            return
        tag = _tag.decode("utf-8")
        tag = re.sub(r"\s+", "", tag)

        (rv, _head_sha, _) = self.run("git rev-parse HEAD")
        if rv != 0 or not _head_sha:
            return
        head_sha = _head_sha.decode("utf-8")
        head_sha = re.sub(r"\s+", "", head_sha)

        version = tag
        if head_sha != tag_sha:
            version += "+" + head_sha[:8]

        tpl = self.run("git diff-index --quiet HEAD --")
        if tpl[0] != 0:
            version += "-dev"
        else:
            tpl = self.run("git diff-index --quiet --cached HEAD --")
            if tpl[0] != 0:
                version += "-dev"

        print(f"law-scraper - version {version}")

    def xpath(self, xpath: str) -> Any:
        """doc me"""
        try:
            elem = self.driver.find_element(By.XPATH, xpath)
            return elem.text
        except Exception:
            return ""

    def main(self) -> Retval:
        """doc me"""
        self.version()
        if not self.toc_code or not self.division or not self.part:
            self.usage()
        self.init_driver()
        self.merger = PdfMerger()
        logging.info(
            "Generating PDFs for code %s, division %s, part %s",
            self.toc_code.upper(),
            self.division,
            self.part,
        )  # noqa
        if not os.path.isdir(ERR_DIR):
            os.mkdir(ERR_DIR)
        if not os.path.isdir(HTML_DIR):
            os.mkdir(HTML_DIR)
        if not os.path.isdir(PDF_DIR):
            os.mkdir(PDF_DIR)
        if not os.path.isdir(self.downloads_dir):
            os.mkdir(self.downloads_dir)

        division = self.division
        part = self.part
        if division[-1] != ".":
            division += "."
        if part[-1] != ".":
            part += "."

        url = URL_MASK % (self.toc_code.upper(), division, part)

        titles = self.get_section_titles(url)
        skip_first = titles["PART"] != ""
        if skip_first:
            rv = self.append_pdf(url, titles["PART"])
            if rv == Retval.OK_FILE_ALREADY_EXISTS:
                return Retval.OK
            if rv == Retval.ERROR:  # failed to create/append pdf
                return Retval.ERROR

        urls = self.get_urls(skip_first)
        retval = self.get_pdfs(urls)
        if self.driver is not None:
            self.driver.close()
            self.driver = None
        self.url = ""
        if retval:
            return Retval.OK
        return Retval.ERROR

    def get_urls(self, skip_first: bool) -> list[dict[str, str]]:
        """doc me"""
        urls: list[dict[str, str]] = []

        xpath = EXPANDEDBRANCHCODESIDA_XPATH
        a_tags = self.driver.find_elements(By.XPATH, xpath)  # noqa
        for a_tag in a_tags:
            if skip_first:
                logging.debug("Skipping first entry")
                skip_first = False
                continue
            url = a_tag.get_attribute("href")
            title = self.parse_url_for_id(r"title=(\d+)", url)
            chapter = self.parse_url_for_id(r"chapter=(\d+)", url)
            if title == "" and chapter == "":
                logging.debug("Skipping %s", url)
                continue
            title2 = re.split(r"\n", a_tag.text)
            if len(title2) > 1:
                title = f"{title2[0]} [{title2[1]}]"
            else:
                title = title2[0]
            # see https://leginfo.legislature.ca.gov/faces/feedbackDetail.xhtml?primaryFeedbackId=prim1641760446147  # noqa
            url = re.sub(r"op_chapter=860&", "", url)
            hsh = {
                "title": title,
                "url": url,
            }
            logging.debug("title=%-50s url=%s", title, url)
            urls.append(hsh)
        return urls

    def get_pdfs(self, urls: list[dict[str, str]]) -> bool:
        """doc me"""
        merge = True
        for hsh in urls:
            url = hsh["url"]
            title = hsh["title"]
            logging.debug("title=%-50s url=%s", title, url)
            rv = self.append_pdf(url, title)
            if rv == Retval.OK_FILE_ALREADY_EXISTS:  # file already exists
                continue
            if rv == Retval.ERROR:  # failed to create/append pdf
                if merge:
                    logging.warning("Skipping merging as %s failed", url)
                merge = False

        if merge:
            # if os.path.exists(self.output_pdf):
            #     os.remove(self.output_pdf)
            logging.info("Merging %d PDFs to %s", len(self.pdfs), self.output_pdf)  # noqa
            if len(self.pdfs) == 1:
                shutil.copy(self.pdfs[0], self.output_pdf)
            else:
                self.merger.write(self.output_pdf)
            self.merger.close()
            self.merger = None

        # fails on Windows so why bother:
        # for pdf in self.pdfs:
        #     try:
        #         os.remove(pdf)
        #     except Exception as exc:
        #         logging.warning("Cannot remove %s: %s", pdf, str(exc))

        return merge


def main() -> int:
    """doc me"""
    scraper = LawScraper()
    if os.environ.get("INPUT_LOG_LEVEL"):
        scraper.set_log_level(str(os.environ.get("INPUT_LOG_LEVEL")))
    if os.environ.get("INPUT_DIVISION"):
        scraper.division = str(os.environ.get("INPUT_DIVISION"))
    if os.environ.get("INPUT_PART"):
        scraper.part = str(os.environ.get("INPUT_PART"))
    if os.environ.get("INPUT_CODE"):
        scraper.toc_code = str(os.environ.get("INPUT_CODE"))
    if len(sys.argv) > 1:
        scraper.division = sys.argv[1]
    if len(sys.argv) > 2:
        scraper.part = sys.argv[2]
    if len(sys.argv) > 3:
        scraper.toc_code = sys.argv[3]
    return scraper.main()


if __name__ == "__main__":
    sys.exit(main())

# cspell:ignore pdfs

# cSpell:ignore EXPANDEDBRANCHCODESID, expandedbranchcodesid, EXPANDEDBRANCHCODESIDA
# cSpell:ignore MANYLAWSECTIONS, manylawsections, pdfkit, Pdfkit, prefs, pypdf
