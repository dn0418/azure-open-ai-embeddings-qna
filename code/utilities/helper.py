import os
import openai
from dotenv import load_dotenv
import logging
import re
import hashlib

from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.llms import AzureOpenAI
from langchain.vectorstores.base import VectorStore
from langchain.chains import ChatVectorDBChain
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.chains.llm import LLMChain
from langchain.chains.chat_vector_db.prompts import CONDENSE_QUESTION_PROMPT
from langchain.document_loaders.base import BaseLoader
from langchain.document_loaders import WebBaseLoader
from langchain.text_splitter import TokenTextSplitter, TextSplitter
from langchain.document_loaders.base import BaseLoader
from langchain.document_loaders import TextLoader

from utilities.formrecognizer import AzureFormRecognizerClient
from utilities.azureblobstorage import AzureBlobStorageClient
from utilities.translator import AzureTranslatorClient
from utilities.customprompt import PROMPT
from utilities.redis import RedisExtended

import pandas as pd
import urllib

class LLMHelper:
    def __init__(self,
        document_loaders : BaseLoader = None, 
        text_splitter: TextSplitter = None,
        embeddings: OpenAIEmbeddings = None,
        llm: AzureOpenAI = None,
        vector_store: VectorStore = None,
        k: int = None,
        pdf_parser: AzureFormRecognizerClient = None,
        blob_client: AzureBlobStorageClient = None,
        enable_translation: bool = False,
        translator: AzureTranslatorClient = None):

        load_dotenv()
        openai.api_type = "azure"
        openai.api_base = os.getenv('OPENAI_API_BASE')
        openai.api_version = "2022-12-01"
        openai.api_key = os.getenv("OPENAI_API_KEY")

        self.api_base = openai.api_base
        self.api_version = openai.api_version
        self.index_name: str = "embeddings"
        self.model: str = os.getenv('OPENAI_EMBEDDINGS_ENGINE_DOC', "text-embedding-ada-002")
        self.deployment_name: str = os.getenv("OPENAI_ENGINE", os.getenv("OPENAI_ENGINES", "text-davinci-003"))
        self.vectore_store_address: str = os.getenv('REDIS_ADDRESS') if os.getenv('REDIS_ADDRESS') else "redis://localhost"
        self.vector_store_port: int= int(os.getenv('REDIS_PORT', 6379))
        # Add protocol (redis://) if the address doesn't start with it
        if not self.vectore_store_address.startswith("redis://"):
            self.vectore_store_address = f"redis://{self.vectore_store_address}"
        self.chunk_size = int(os.getenv('CHUNK_SIZE', 500))
        self.chunk_overlap = int(os.getenv('CHUNK_OVERLAP', 100))

        self.document_loaders: BaseLoader = WebBaseLoader if document_loaders is None else document_loaders
        self.text_splitter: TextSplitter = TokenTextSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap) if text_splitter is None else text_splitter
        self.embeddings: OpenAIEmbeddings = OpenAIEmbeddings(model=self.model, chunk_size=1) if embeddings is None else embeddings
        self.llm: AzureOpenAI = AzureOpenAI(deployment_name=self.deployment_name) if llm is None else llm
        self.vector_store: RedisExtended = RedisExtended(redis_url=f"{self.vectore_store_address}:{self.vector_store_port}", index_name=self.index_name, embedding_function=self.embeddings.embed_query) if vector_store is None else vector_store   
        self.k : int = 3 if k is None else k

        self.pdf_parser : AzureFormRecognizerClient = AzureFormRecognizerClient() if pdf_parser is None else pdf_parser
        self.blob_client: AzureBlobStorageClient = AzureBlobStorageClient() if blob_client is None else blob_client
        self.enable_translation : bool = False if enable_translation is None else enable_translation
        self.translator : AzureTranslatorClient = AzureTranslatorClient() if translator is None else translator

    def add_embeddings_lc(self, source_url):
        try:
            documents = self.document_loaders(source_url).load()
            docs = self.text_splitter.split_documents(documents)
            keys = []
            for i, doc in enumerate(docs):
                # Create a unique key for the document
                source_url = source_url.split('?')[0]
                filename = "/".join(source_url.split('/')[4:])
                hash_key = hashlib.sha1(f"{source_url}_{i}".encode('utf-8')).hexdigest()
                keys.append(hash_key)
                doc.metadata = {"source": f"[{source_url}]({source_url}_SAS_TOKEN_PLACEHOLDER_)" , "chunk": i, "key": hash_key, "filename": filename}
            self.vector_store.add_documents(documents=docs, redis_url="redis://localhost:6379",  index_name=self.index_name, keys=keys)
        except Exception as e:
            logging.error(f"Error adding embeddings for {source_url}: {e}")
            raise e

    def convert_file_and_add_embeddings(self, source_url, filename, enable_translation=False):
        # Extract the text from the file
        text = self.pdf_parser.analyze_read(source_url)
        # Translate if requested
        text = list(map(lambda x: self.translator.translate(x), text)) if self.enable_translation else text

        # Upload the text to Azure Blob Storage
        converted_filename = f"converted/{filename}.txt"
        source_url = self.blob_client.upload_file("\n".join(text), f"converted/{filename}.txt", content_type='text/plain')

        print(f"Converted file uploaded to {source_url} with filename {filename}")
        # Update the metadata to indicate that the file has been converted
        self.blob_client.upsert_blob_metadata(filename, {"converted": "true"})

        self.add_embeddings_lc(source_url=source_url)

        return converted_filename

    def get_all_documents(self, k: int = None):
        result = self.vector_store.similarity_search(query="*", k= k if k else self.k)
        return pd.DataFrame(list(map(lambda x: {
                'key': x.metadata['key'],
                'filename': x.metadata['filename'],
                'source': urllib.parse.unquote(x.metadata['source']), 
                'content': x.page_content, 
                'metadata' : x.metadata,
                }, result)))

    def get_semantic_answer_lang_chain(self, question, chat_history):
        question_generator = LLMChain(llm=self.llm, prompt=CONDENSE_QUESTION_PROMPT, verbose=False)
        doc_chain = load_qa_with_sources_chain(self.llm, chain_type="stuff", verbose=False, prompt=PROMPT)
        chain = ChatVectorDBChain(
            vectorstore=self.vector_store,
            question_generator=question_generator,
            combine_docs_chain=doc_chain,
            return_source_documents=True,
            top_k_docs_for_context= self.k
        )
        result = chain({"question": question, "chat_history": chat_history})
        context = "\n".join(list(map(lambda x: x.page_content, result['source_documents'])))
        sources = "\n".join(set(map(lambda x: x.metadata["source"], result['source_documents'])))

        container_sas = self.blob_client.get_container_sas()
        
        result['answer'] = result['answer'].split('SOURCES:')[0].split('Sources:')[0].split('SOURCE:')[0].split('Source:')[0]
        sources = sources.replace('_SAS_TOKEN_PLACEHOLDER_', container_sas)

        return question, result['answer'], context, sources

    def get_embeddings_model(self):
        OPENAI_EMBEDDINGS_ENGINE_DOC = os.getenv('OPENAI_EMEBDDINGS_ENGINE', os.getenv('OPENAI_EMBEDDINGS_ENGINE_DOC', 'text-embedding-ada-002'))  
        OPENAI_EMBEDDINGS_ENGINE_QUERY = os.getenv('OPENAI_EMEBDDINGS_ENGINE', os.getenv('OPENAI_EMBEDDINGS_ENGINE_QUERY', 'text-embedding-ada-002'))
        return {
            "doc": OPENAI_EMBEDDINGS_ENGINE_DOC,
            "query": OPENAI_EMBEDDINGS_ENGINE_QUERY
        }

    def get_completion(self, prompt, **kwargs):
        return self.llm(prompt)
