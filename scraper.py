"""
Copyright (c) 2022, Ross Smith II
SPDX-License-Identifier: MIT
"""

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
from subprocess import PIPE, Popen

import pdfkit
from PyPDF2 import PdfFileMerger, PdfFileReader
from selenium import webdriver
from selenium.webdriver.common.by import By

DEFAULT_TOC_CODE = "CIV"

# pylint: disable=C0301
# https://leginfo.legislature.ca.gov/faces/codes_displayexpandedbranch.xhtml?tocCode=CIV&division=1.&title=&part=1.&chapter=&article=

# pylint: disable=C0301
URL_MASK = "https://leginfo.legislature.ca.gov/faces/codes_displayexpandedbranch.xhtml?tocCode=%s&division=%s&title=&part=%s&chapter=&article="  # noqa

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


# pylint: disable=R0902
class LawScraper:
    """doc me"""

    def __init__(self):
        """class initializer"""
        # pylint: disable=W0235
        super().__init__()
        self.ci = os.getenv("CI") is not None
        self.division = None
        self.downloads_dir = os.path.expanduser("~/Downloads")
        self.driver = None
        self.has_tidy = shutil.which("tidy")
        self.log_level = logging.INFO
        self.merger = None
        self.next_page = 0
        self.output_pdf = ""
        self.parents = {}
        self.part = None
        self.pdfs = []
        self.toc_code = DEFAULT_TOC_CODE
        self.url = ""

        logging.root.setLevel(self.log_level)
        requests_log = logging.getLogger("urllib3")
        requests_log.setLevel(logging.ERROR)

    def init_driver(self):
        """doc me"""

        appState = {
            "recentDestinations": [{"id": "Save as PDF", "origin": "local", "account": ""}],
            "selectedDestinationId": "Save as PDF",
            "version": 2,
        }

        prefs = {
            "download.default_directory": self.downloads_dir,
            "profile.default_content_settings.popups": 0,
            "printing.print_preview_sticky_settings.appState": json.dumps(appState),  # noqa
        }

        options = webdriver.ChromeOptions()
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--kiosk-printing")
        options.add_argument("--log-level=3")
        if self.ci:
            # see https://github.com/rasa/law-scraper/runs/4740875776?check_suite_focus=true#step:8:8  # noqa
            # see https://stackoverflow.com/a/47887610/1432614
            options.add_argument("--no-sandbox")
            options.add_argument("--headless")
        self.driver = webdriver.Chrome(options=options)

        webdriver.remote.remote_connection.LOGGER.setLevel(logging.CRITICAL)

    def append_pdf(self, url, title):
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
            self.merger.append(pdf, import_bookmarks=False)
        except Exception as exc:
            logging.error("Merging %s failed: %s", pdf, str(exc))
            return Retval.ERROR

        bm = self.merger.addBookmark(title, self.next_page, parent)
        self.parents[prefix] = bm
        self.next_page += self.get_num_pages(pdf)
        return Retval.OK

    def get(self, url):
        """doc me"""
        if self.url == url:
            return True
        self.url = url
        logging.debug("Retrieving %s", url)
        self.driver.get(url)
        return True

    @staticmethod
    def get_num_pages(pdf):
        """doc me"""
        reader = PdfFileReader(open(pdf, "rb"), strict=False)
        return reader.getNumPages()

    def get_pdf(self, url, title):
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
    def get_prefix(title):
        """doc me"""
        parts = re.split(r"\s+", title)
        return parts[0]

    def get_section_numbers(self, url):
        """doc me"""
        rv = {}
        for section in SECTION_NAMES:
            num = self.parse_url_for_id(r"%s=([^&]+)" % section.lower(), url)
            if num:
                if num[-1] == ".":
                    num = num[:-1]
            rv[section] = num
        return rv

    def get_section_titles(self, url):
        """doc me"""
        if url:
            self.get(url)

        # See https://stackoverflow.com/a/16608016/1432614
        rv = {}
        for section in SECTION_NAMES:
            xpath = CONTAINS_TEXT_XPATH % section
            result = self.xpath(xpath)
            if re.match(section, result) is None:
                result = ""
            rv[section] = result

        return rv

    @staticmethod
    def normalize(path):
        """doc me"""
        path = path.replace(" - ", "-")
        path = re.sub(r"\. ", " ", path)
        # Invalid characters on windows
        path = re.sub(r"[<>:\"/\\|\?\*]+", " ", path)
        path = re.sub(r"\s+", "-", path)
        path = path.lower()
        return path

    @staticmethod
    def parse_url_for_id(regex, url):
        """doc me"""
        m = re.search(regex, url)
        if m is None:
            return ""
        return m.group(1)

    def print_pdf(self, pdf):
        """doc me"""
        if self.ci:
            # Not working yet using headless Chrome
            return False

        logging.debug("Generating %s (using Chrome)", pdf)

        mask = os.path.join(self.downloads_dir, "*.pdf")
        old = sorted(glob.iglob(mask), key=os.path.getmtime, reverse=True)
        if old:
            old = old[0]
        else:
            old = ""

        self.driver.execute_script("window.print();")
        time.sleep(5)
        new = sorted(glob.iglob(mask), key=os.path.getmtime, reverse=True)
        if new:
            new = new[0]
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
    def run(cmd):
        """doc me"""
        args = shlex.split(cmd)
        if re.search(r"/\\", args[0]) is None:
            full_path = shutil.which(args[0])
            if full_path:
                args[0] = full_path
        with Popen(args, stdout=PIPE, stderr=PIPE) as process:
            (out, err) = process.communicate()
            rv = process.wait()
        return (rv, out, err)

    def save_html(self, prefix, html):
        """doc me"""
        if os.path.exists(html):
            return True

        while True:
            try:
                xpath = MANYLAWSECTIONS_XPATH
                elem = self.driver.find_element(By.XPATH, xpath)
                break
            except Exception:
                pass

            try:
                xpath = EXPANDEDBRANCHCODESID_XPATH
                elem = self.driver.find_element(By.XPATH, xpath)
                break
            except Exception:
                logging.warning("No element for %s found for %s", xpath, prefix)  # noqa
                html = "%s/%s.html" % (ERR_DIR, prefix)
                logging.debug("Saving %s", html)
                with open(html, "w") as fh:
                    fh.write(self.driver.page_source)
                return False

        inner_html = elem.get_attribute("innerHTML")

        try:
            logging.debug("Saving %s", html)
            with open(html, "w") as fh:
                fh.write(inner_html)
            self.tidy(html)
        except Exception as exc:
            logging.warning("Cannot save %s: %s", html, str(exc))
        return True

    def save_pdf(self, prefix):
        """doc me"""

        pdf = os.path.join(self.downloads_dir, prefix + ".pdf")

        html = "%s/%s.html" % (HTML_DIR, prefix)
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
    def set_log_level(level):
        """doc me"""
        if not level:
            return
        if isinstance(level, str):
            level = level.upper()
        logging.root.setLevel(level)

    def set_output_name(self, url, title):
        """doc me"""
        numbers = self.get_section_numbers(url)
        self.output_pdf = "%s/%s_division-%s_%s.pdf" % (
            PDF_DIR,
            self.toc_code.lower(),
            numbers["DIVISION"],
            self.normalize(title),
        )
        return self.output_pdf

    def tidy(self, html):
        """doc me"""
        if not self.has_tidy:
            return False

        try:
            logging.debug("Tidying %s", html)
            (rv, _, err) = self.run('tidy -config config.tidy --write-back yes "%s"' % html)  # noqa
            if rv != 0:
                logging.warning("Tidy returned error %s processing %s: %s", rv, html, err)
                return False

            with open(html, "r+") as fh:
                html_data = fh.read()
                html_data = re.sub(
                    r"h6.c7 {float: left",
                    "h6.c7 {display: inline; font-size: 100%",
                    html_data,
                )
                html_data = re.sub(r" href=\"[^\"]*\"", "", html_data)
                fh.seek(0)
                fh.write(html_data)
                fh.truncate()
        except Exception as exc:
            logging.warning("Cannot tidy %s: %s", html, str(exc))
            return False

        return True

    @staticmethod
    def usage():
        """doc me"""
        print("Usage: %s division part" % sys.argv[0], file=sys.stderr)
        sys.exit(1)

    def version(self):
        """doc me"""
        (rv, tag_sha, _) = self.run("git rev-list --tags --max-count=1")
        if rv != 0 or not tag_sha:
            return
        tag_sha = tag_sha.decode("utf-8")
        tag_sha = re.sub(r"\s+", "", tag_sha)

        (rv, tag, _) = self.run("git describe --tags " + tag_sha)
        if rv != 0 or not tag:
            return
        tag = tag.decode("utf-8")
        tag = re.sub(r"\s+", "", tag)

        (rv, head_sha, _) = self.run("git rev-parse HEAD")
        if rv != 0 or not head_sha:
            return
        head_sha = head_sha.decode("utf-8")
        head_sha = re.sub(r"\s+", "", head_sha)

        version = tag
        if head_sha != tag_sha:
            version += "+" + head_sha[:8]

        tpl = self.run("git diff-index --quiet HEAD --")
        if tpl[0] != 0:
            version += "-dirty"
        else:
            tpl = self.run("git diff-index --quiet --cached HEAD --")
            if tpl[0] != 0:
                version += "-dirty"

        print("law-scraper - version %s" % version)

    def xpath(self, xpath):
        """doc me"""
        try:
            elem = self.driver.find_element(By.XPATH, xpath)
            return elem.text
        except Exception:
            return ""

    def main(self):
        """doc me"""
        self.version()
        if not self.toc_code or not self.division or not self.part:
            self.usage()
        self.init_driver()
        self.merger = PdfFileMerger()
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
        rv = self.get_pdfs(urls)
        if self.driver:
            self.driver.close()
            self.driver = None
        self.url = ""
        if rv:
            return Retval.OK
        return Retval.ERROR

    def get_urls(self, skip_first):
        """doc me"""
        urls = []

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
            title = re.split(r"\n", a_tag.text)
            if len(title) > 1:
                title = "%s [%s]" % (title[0], title[1])
            else:
                title = title[0]
            # see https://leginfo.legislature.ca.gov/faces/feedbackDetail.xhtml?primaryFeedbackId=prim1641760446147  # noqa
            url = re.sub(r"op_chapter=860&", "", url)
            hsh = {
                "title": title,
                "url": url,
            }
            logging.debug("title=%-50s url=%s", title, url)
            urls.append(hsh)
        return urls

    def get_pdfs(self, urls):
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


def main():
    """doc me"""
    scraper = LawScraper()
    scraper.set_log_level(os.getenv("INPUT_LOG_LEVEL"))
    if os.getenv("INPUT_DIVISION") is not None:
        scraper.division = os.getenv("INPUT_DIVISION")
    if os.getenv("INPUT_PART") is not None:
        scraper.part = os.getenv("INPUT_PART")
    if os.getenv("INPUT_CODE") is not None:
        scraper.toc_code = os.getenv("INPUT_CODE")
    if len(sys.argv) > 1:
        scraper.division = sys.argv[1]
    if len(sys.argv) > 2:
        scraper.part = sys.argv[2]
    if len(sys.argv) > 3:
        scraper.toc_code = sys.argv[3]
    return scraper.main()


if __name__ in ["__main__", "<run_path>"]:
    # pylint: disable=C0103
    exitval = int(main())
    if __name__ == "__main__":
        sys.exit(exitval)
