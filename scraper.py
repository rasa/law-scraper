"""
Copyright (c) 2022, Ross Smith II
SPDX-License-Identifier: MIT
"""

import glob
import json
import logging
import os
import re
import shutil
import sys
import time
import pdfkit
from PyPDF2 import PdfFileMerger, PdfFileReader
from selenium import webdriver

DEFAULT_TOC_CODE = "CIV"

# pylint: disable=C0301
# https://leginfo.legislature.ca.gov/faces/codes_displayexpandedbranch.xhtml?tocCode=CIV&division=4.&title=&part=5.&chapter=&article=

# pylint: disable=C0301
URL_MASK = "https://leginfo.legislature.ca.gov/faces/codes_displayexpandedbranch.xhtml?tocCode=%s&division=%s&title=&part=%s&chapter=&article="  # noqa

SECTION_NAMES = ["DIVISION", "PART", "TITLE", "CHAPTER", "ARTICLE"]


# pylint: disable=R0902
class LawScraper:
    """doc me"""

    def __init__(self):
        """class initializer"""
        # pylint: disable=W0235
        super().__init__()
        self.downloads_dir = os.path.expanduser("~/Downloads")
        if not os.path.isdir(self.downloads_dir):
            os.mkdir(self.downloads_dir)
        self.merger = PdfFileMerger()
        self.next_page = 0
        self.output_pdf = ""
        self.parents = {}
        self.pdfs = []
        self.toc_code = DEFAULT_TOC_CODE
        self.division = 1
        self.part = 1

        appState = {
            "recentDestinations": [
                {"id": "Save as PDF", "origin": "local", "account": ""}
            ],
            "selectedDestinationId": "Save as PDF",
            "version": 2,
        }

        prefs = {
            "download.default_directory": self.download_dir,
            "profile.default_content_settings.popups": 0,
            "printing.print_preview_sticky_settings.appState": json.dumps(
                appState
            )  # noqa
        }

        options = webdriver.ChromeOptions()
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--kiosk-printing")
        options.add_argument("--log-level=3")
        # see https://github.com/rasa/law-scraper/runs/4740875776?check_suite_focus=true#step:8:8
        # see https://stackoverflow.com/a/47887610/1432614
        options.add_argument('--no-sandbox')
        options.add_argument('--headless')
        self.driver = webdriver.Chrome(options=options)

        logging.root.setLevel(logging.DEBUG)
        requests_log = logging.getLogger("urllib3")
        requests_log.setLevel(logging.ERROR)

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
            return True

        pdf = self.get_pdf(url, title)
        if not pdf:
            return False
        self.pdfs.append(pdf)
        try:
            logging.debug("Merging %s", pdf)
            self.merger.append(pdf)
        except Exception as exc:
            logging.error("Merging %s failed: %s", pdf, str(exc))
            return False

        bm = self.merger.addBookmark(title, self.next_page, parent)
        self.parents[prefix] = bm
        self.next_page += self.get_num_pages(pdf)
        return True

    @staticmethod
    def get_num_pages(pdf):
        """doc me"""
        reader = PdfFileReader(open(pdf, "rb"), strict=False)
        return reader.getNumPages()

    def get_pdf(self, url, title):
        """doc me"""
        logging.debug("Retrieving %s", url)
        self.driver.get(url)

        filename = self.normalize(title)
        numbers = self.get_section_numbers(url)
        for section in reversed(SECTION_NAMES):
            if numbers[section]:
                filename = numbers[section] + "_" + filename
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
            num = self.parse_url_for_id(r"%s=([\d.]+)" % section.lower(), url)
            if num:
                if num[-1] == ".":
                    num = num[:-1]
            rv[section] = num
        return rv

    def get_section_titles(self, url):
        """doc me"""
        if url:
            logging.debug("Retrieving %s", url)
            self.driver.get(url)

        # See https://stackoverflow.com/a/16608016/1432614
        xpath_mask = '//*[contains(text(),"%s ")]'
        rv = {}
        for section in SECTION_NAMES:
            _xpath = xpath_mask % section
            result = self.xpath(_xpath)
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

    # pylint: disable=R0915
    def save_pdf(self, prefix):
        """doc me"""

        n = 0
        name = prefix
        while True:
            pdf = os.path.join(self.downloads_dir, name + ".pdf")
            if not os.path.exists(pdf):
                break
            n += 1
            name = "%s_%d" % (prefix, n)

        html = "html/%s.html" % prefix
        try:
            xpath = '//*[@id="manylawsections"]'
            elem = self.driver.find_element_by_xpath(xpath)
            if not elem:
                logging.warning("No element for %s found for %s", xpath, prefix)  # noqa
            else:
                inner_html = elem.get_attribute("innerHTML")
                if not os.path.exists(html):
                    logging.debug("Saving %s", html)
                    with open(html, "w") as fh:
                        fh.write(inner_html)
                    if shutil.which("tidy"):
                        cmd = (
                            'tidy -config config.tidy --write-back yes "%s"' % html
                        )  # noqa
                        os.system(cmd)
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
            logging.warning("Cannot save %s: %s", html, str(exc))

        if os.path.isfile(pdf):
            logging.info("File already exists: %s", pdf)
            return pdf

        logging.debug("Saving %s", pdf)

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

        if old == new:
            if not os.path.isfile(html):
                logging.warning("No new pdf found in %s", self.downloads_dir)
                return False
            pdfkit.from_file(html, pdf)
            if not os.path.isfile(pdf):
                logging.warning("Failed to conver %s to %s", html, pdf)
                return False

        # if os.path.isfile(pdf):
        #     logging.debug("Deleting %s", pdf)
        #     try:
        #         os.remove(pdf)
        #     except Exception:
        #         pass

        if new and os.path.isfile(new):
          logging.debug("Moving %s to %s", new, pdf)
          os.rename(new, pdf)

        return pdf

    def set_output_name(self, url, title, ext=".pdf"):
        """doc me"""
        numbers = self.get_section_numbers(url)
        self.output_pdf = "pdf/%s_division-%s_%s%s" % (
            self.toc_code.lower(),
            numbers["DIVISION"],
            self.normalize(title),
            ext,
        )
        return self.output_pdf

    @staticmethod
    def usage():
        """doc me"""
        print("Usage: %s division part" % sys.argv[0], file=sys.stderr)
        sys.exit(1)

    def xpath(self, xpath):
        """doc me"""
        try:
            elem = self.driver.find_element_by_xpath(xpath)
            return elem.text
        except Exception:
            return ""

    def main(self):
        """doc me"""
        if not self.division or not self.part:
            self.usage()
        if not os.path.isdir("html"):
            os.mkdir("html")
        if not os.path.isdir("pdf"):
            os.mkdir("pdf")

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
            if not rv:
                return 0

        urls = self.get_urls(skip_first)
        rv = self.get_pdfs(urls)
        if self.driver:
            self.driver.close()
            self.driver = None
        if rv:
            return 0
        return 1

    def get_urls(self, skip_first):
        """doc me"""
        urls = []

        expandedbranchcodesid_xpath = (
            '//*[@id="expandedbranchcodesid"]/*/a[@href]'  # noqa
        )
        a_tags = self.driver.find_elements_by_xpath(expandedbranchcodesid_xpath)  # noqa
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
            if not self.append_pdf(url, title):
                merge = False

        if merge:
            # if os.path.exists(self.output_pdf):
            #     os.remove(self.output_pdf)
            logging.info(
                "Merging %d PDFs to %s", len(urls) + 1, self.output_pdf
            )  # noqa
            self.merger.write(self.output_pdf)
            self.merger.close()
            self.merger = None

        for pdf in self.pdfs:
            try:
                os.remove(pdf)
            except Exception as exc:
                logging.warning("Cannot remove %s: %s", pdf, str(exc))

        return merge


def main():
    """doc me"""
    scraper = LawScraper()
    if len(sys.argv) < 3:
        scraper.usage()
    scraper.division = sys.argv[1]
    scraper.part = sys.argv[2]
    if len(sys.argv) > 3:
        scraper.toc_code = sys.argv[3]
    return scraper.main()


if __name__ in ["__main__", "<run_path>"]:
    # pylint: disable=C0103
    exitval = main()
    if __name__ == "__main__":
        sys.exit(exitval)
