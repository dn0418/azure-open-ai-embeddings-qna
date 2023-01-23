from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient
import os

PAGES_PER_FILE = 4
SECTION_TO_EXCLUDE = ['title', 'sectionHeading', 'footnote', 'pageHeader', 'pageFooter', 'pageNumber']

def analyze_read(formUrl):

    document_analysis_client = DocumentAnalysisClient(
        endpoint=os.environ['FORM_RECOGNIZER_ENDPOINT'], credential=AzureKeyCredential(os.environ['FORM_RECOGNIZER_KEY'])
    )
    
    poller = document_analysis_client.begin_analyze_document_from_url(
            "prebuilt-layout", formUrl)
    layout = poller.result()

    results = []
    page_result = ''
    for p in layout.paragraphs:
        page_number = p.bounding_regions[0].page_number
        output_file_id = int((page_number - 1 ) / PAGES_PER_FILE)

        if len(results) < output_file_id + 1:
            results.append('')

        if p.role not in SECTION_TO_EXCLUDE:
            results[output_file_id] += f"{p.content}\n"

    return results
