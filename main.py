import sys
sys.path.append("..") 
from sentence_transformers import SentenceTransformer , util
from fastapi import FastAPI , HTTPException ,Query, UploadFile, File, Form , Header, Depends
from database import jobs_collection ,users_collection, rag_collection, rag_names_collection
from pydantic import BaseModel,validator,field_validator
import torch
# from sklearn.feature_extraction.text import TfidfVectorizer
# from sklearn.metrics.pairwise import cosine_similarity
import re
import fitz  # PyMuPDf
from io import BytesIO
# from googleapiclient.discovery import build
from loguru import logger
from functools import lru_cache
import requests  # To download Cloudinary files
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
# from bson.errors import InvalidId
security = HTTPBearer()
from fastapi import Request, BackgroundTasks
import json
from langchain.text_splitter import RecursiveCharacterTextSplitter
import os
from datetime import datetime
import time
import openai
from dotenv import load_dotenv

from PIL import Image
from io import BytesIO
# import face_recognition
import requests
# import tempfile
# import traceback
# from deepface import DeepFace
# import easyocr
from contextlib import asynccontextmanager
load_dotenv()


# from some_fraud_detection_lib import is_fake_id
# from some_id_front_detector import is_front_side





DJANGO_AUTH_URL = "http://localhost:8000/api/token/verify/"

security = HTTPBearer()
# app = FastAPI()
#load  ats model

# model_1 = SentenceTransformer("all-MiniLM-L6-v2")

#load recommender model
# def load_model():
#     # device = "cuda" if torch.cuda.is_available() else "cpu"
#     return SentenceTransformer("paraphrase-MiniLM-L6-v2", device="cpu")

# @app.on_event("startup")
# async def startup_event():
#     global model
#     model = load_model()
#     logger.info("ML Model Loaded Successfully!")
#     load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    global model, ats_model
    # Load main recommendation model
    model = SentenceTransformer("paraphrase-MiniLM-L6-v2", device="cpu")
    logger.info("Recommendation model loaded.")
    # Load ATS model
    # ats_model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
    logger.info("ATS model loaded.")
    yield
    # Shutdown logic (if any)
    logger.info("Shutting down FastAPI application.")

app = FastAPI(lifespan=lifespan)



CLOUDINARY_CLOUD_NAME ="dkvyfbtdl"
def extract_text_from_pdf_cloud(public_id: str):
    """Downloads and extracts text from a Cloudinary-hosted PDF file using its public_id."""
    try:
        pdf_url = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/raw/upload/{public_id}.pdf"
        print("Downloading PDF from:", pdf_url)

        response = requests.get(pdf_url)
        response.raise_for_status()  # Ensure the request was successful
        print("PDF downloaded successfully.")
       
        pdf_stream = BytesIO(response.content)
        doc = fitz.open(stream=pdf_stream, filetype="pdf")
        
        text = "\n".join([page.get_text("text") for page in doc])
        # print ("text",text)
        doc.close()

        return text.strip() if text else "No text found in PDF."
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to download PDF: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF processing error: {e}")   




def get_embedding(text):
    with torch.no_grad():
        return model.encode(text, convert_to_tensor=True).tolist()
    
def format_cv_url(cv_url):
    if cv_url.startswith("http"):
        return cv_url.split("/")[-1]
    return cv_url


@app.get("/recom/")
async def recommend_jobs(user_id: int, cv_url: str, page: int = 1, page_size: int = 10):
    try:
        print("user_id",user_id)
        cv_url = format_cv_url(cv_url)
        print("cv_url",cv_url)
        print("page",page)
        if page < 1:
            raise HTTPException(status_code=400, detail="Page number must be 1 or higher")

        user_data = users_collection.find_one({"user_id": user_id})
        page_count = jobs_collection.count_documents({"status": "1"})
        # page_count= jobs_collection.count_documents({})        # page_count = page_size + 1
        print("page_count",page_count)
        if page_count == 0:
            raise HTTPException(status_code=404, detail="No jobs found")
        if page*page_size > 100:
            raise HTTPException(status_code=400, detail="Max recommendations is 100")
        if page > page_count/page_size and page > 1:
            raise HTTPException(status_code=400, detail="Page number exceeds available pages")
        if not cv_url:
            raise HTTPException(status_code=400, detail="CV URL is required")
        if not user_id:
            raise HTTPException(status_code=400, detail="User ID is required")
        if user_data:
            stored_cv_url = user_data.get("cv_url")
            if stored_cv_url == cv_url:
                print("Using stored embedding (CV unchanged)")
                query_vector = user_data["embedding"]
            else:
                print("CV updated, generating new embedding")
                extracted_text = extract_text_from_pdf_cloud(cv_url)
                # print ("extracted_text",extracted_text)
                query_vector = get_embedding(extracted_text)
                users_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {"embedding": query_vector, "cv_url": cv_url}}  
                )
        else:
            print("New user, extracting CV text")
            extracted_text = extract_text_from_pdf_cloud(cv_url)
            query_vector = get_embedding(extracted_text)
            users_collection.insert_one(
                {"user_id": user_id, "embedding": query_vector, "cv_url": cv_url} 
            )
        skip = (page - 1) * page_size
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "vector",
                    "path": "combined_embedding",
                    "queryVector": query_vector,
                    "numCandidates": 500,
                    "limit": 100,
                    "metric": "cosine"
                }
                
            },
            {
                "$match": {
                    "status": "1",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "title": 1,
                    "company_logo": 1,
                    "description": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            },
            {"$skip": skip}, 
            {"$limit": page_size}
        ]

        results = list(jobs_collection.aggregate(pipeline))

        if not results:
            raise HTTPException(status_code=404, detail="No matching jobs found")

        return {
            "page": page,
            "page_size": page_size,
            "total_results": page_count if page_count < 100 else 100,
            "recommendations": results
        }

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

class Job(BaseModel):
    id: int
    title: str
    description: str
    location: str
    experince: str
    status: str
    type_of_job: str
    company: int
    company_name: str
    company_logo: Optional[str] = None
    {'id': 31, 'title': 'Backend Engineer', 'description': 'Django and FastAPI experience required', 'location': 'Remote', 'status': 'open'
     , 'type_of_job': 'Full-time', 'experince': 'Mid-level', 'company': 8, 'company_name': 'Aisha Amr', 'company_logo': None}
    

    # @validator("title", "description")
    # def must_not_be_empty(cls, value):
    #     if not value.strip():
    #         raise ValueError("Field cannot be empty")
    #     return value

    # @validator("id")
    # def must_have_id(cls, value):
    #     if not value:
    #         raise ValueError("Must include job ID")
    #     return value
    
    @field_validator("title", "description")
    @classmethod
    def non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field cannot be empty")
        return v

    @field_validator("id")
    @classmethod
    def must_have_id(cls, v: int) -> int:
        if v is None:
            raise ValueError("Must include job ID")
        return value
def recommend_emails(job):
    try:
        results = list(users_collection.aggregate([
            {"$match": {"cv_url": {"$exists": True}}},
            {
                "$vectorSearch": {
                    "index": "default",
                    "queryVector": job["combined_embedding"],
                    "path": "embedding",
                    "numCandidates": 1000,
                    "limit": 10,
                    "metric": "cosine"
                }
            }
        ]))
        mail = os.getenv("MAIL_SERVICE")
        front = os.getenv("FRONT")
        requests.post(mail + "/send_recommendation", json={"emails": [user["email"] for user in results], "job_title": job["title"], 'job_link': front + "applicant/jobs/" + str(job["id"]), 'company_name': job["company_name"]})
    except Exception as e:
        print(e)
    return {"message": "Emails sent successfully"}


# @app.post("/jobs", dependencies=[Depends(verify_token)])
@app.post("/jobs")
async def create_job(job: Job, request: Request, background_tasks: BackgroundTasks):
    job_data = job.dict()
    job_data["combined_embedding"] = get_embedding(job_data["description"] + " " + " ".join(job_data["title"]))
    inserted_job = jobs_collection.insert_one(job_data)
    background_tasks.add_task(recommend_emails, job_data)
    return {"id": str(inserted_job.inserted_id),
            "message": "Job created successfully",
            #"mongodb_id": str(inserted_job.inserted_id)
            }


# Commentb l7d m ashof ha7tagha wala la2
# @app.get("/jobs/")
# async def get_jobs(limit: int = 10, skip: int = 0):
#     jobs = list(jobs_collection.find({}, {"_id": 1, "title": 1, "description": 1, "title": 1})
#                 .skip(skip).limit(limit))
#     for job in jobs:
#         job["_id"] = str(job["_id"])
#     return {"jobs": jobs}


# @app.get("/jobs/{job_id}")
# async def get_job(job_id: str):
#     job = jobs_collection.find_one({"_id": ObjectId(job_id)})
#     if not job:
#         raise HTTPException(status_code=404, detail="Job not found")
#     job["_id"] = str(job["_id"])
#     return job

@app.put("/jobs/{job_id}")
async def update_job(job_id: int, job: Job):
    updated_job = job.dict()
    updated_job["combined_embedding"] = get_embedding(updated_job["description"] + " " + " ".join(updated_job["title"]))
    print(job_id)
    existing_job = jobs_collection.find_one({"id": job_id})
    print(existing_job)
    if not existing_job:
            result = jobs_collection.insert_one(updated_job)
            return {"message": "Job created successfully", "id": str(result.inserted_id)}
    result = jobs_collection.update_one({"id": job_id}, {"$set": updated_job})
    # print (result)
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Job not found in mongodb")

    return {"message": "Job updated successfully"}

@app.delete("/jobs/{job_id}")
async def delete_job(job_id: int):
    print("job_id",job_id)
    existing_job = jobs_collection.find_one({"id": job_id})
    print("existing_job",existing_job)
    if not existing_job:
        raise HTTPException(status_code=404, detail="Job not found fastapi mongodb")
    result = jobs_collection.delete_one({"id": job_id})
    print(result)
    print(result)
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Job not found in mongodb")
    return {"message": "Job deleted successfully"}


class ATSRequest(BaseModel):
    cv_url: str
    job_id: int

def get_embedding_ats(text):
    with torch.no_grad():
        return model_1.encode(text, convert_to_tensor=True).tolist()

def preprocess_text(text):
    text = re.sub(r'\W+', ' ', text)  # Remove special characters
    text = re.sub(r'\s+', ' ', text).strip()  # Remove extra spaces
    return text.lower()


def calculate_embedding_similarity(cv_text, job_text):
    cv_embedding = model_1.encode(cv_text, convert_to_tensor=True)
    job_embedding = model_1.encode(job_text, convert_to_tensor=True)
    similarity_score = util.pytorch_cos_sim(cv_embedding, job_embedding).item()
    return similarity_score * 100  


@app.post("/ats/{user_id}/{job_id}/")
async def ats_system(user_id: int , job_id: int, request: ATSRequest):
    
    print(f"Received request for user_id={user_id}, job_id={job_id}")

    try:
        job_object_id = job_id
    except Exception as e:
        print(f"Invalid job_id: {job_id} - Error: {e}")
        raise HTTPException(status_code=400, detail="Invalid job ID")


    job = jobs_collection.find_one({"id": job_object_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job_embedding = job.get("combined_embedding", None)

    if not job_embedding:
        raise HTTPException(status_code=500, detail="Job embedding missing")

    # Get CV URL
    cv_url = format_cv_url(request.cv_url)
    print(f"cv_url={cv_url}")

    
    user_data = users_collection.find_one({"user_id": user_id})

    if user_data and user_data.get("cv_url") == cv_url:
        print("Using stored embedding (CV unchanged)")
        cv_embedding = user_data["embedding"]
    else:
        try:
            extracted_text = extract_text_from_pdf_cloud(cv_url)
            print(f"Extracted text length: {len(extracted_text)}")
        except Exception as e:
            print(f"Error extracting text: {e}")
            raise HTTPException(status_code=500, detail="CV extraction failed")

        try:
            cv_embedding = get_embedding(extracted_text)
            print(f"Generated embedding length: {len(cv_embedding)}")
        except Exception as e:
            print(f"Error generating embedding: {e}")
            raise HTTPException(status_code=500, detail="Embedding generation failed")

        users_collection.update_one(
            {"user_id": user_id},
            {"$set": {"embedding": cv_embedding, "cv_url": cv_url}},
            upsert=True
        )

    
    if not cv_embedding or not job_embedding:
        raise HTTPException(status_code=500, detail="Embedding computation failed")
    
    try:
        similarity_score = util.pytorch_cos_sim(
            torch.tensor(cv_embedding), torch.tensor(job_embedding)
        ).item()
    except Exception as e:
        print(f"Error computing similarity: {e}")
        raise HTTPException(status_code=500, detail="Similarity computation failed")

    return {"match_percentage": round(similarity_score * 100, 2), "message": "Higher score means a better match!"}

# @app.delete("/rag")
# async def delete_rag(name: str, id: str):
#     if id:
#         existing_rag = rag_names_collection.find_one({"_id": id})
#     elif name:
#         existing_rag = rag_names_collection.find_one({"name": name})
#     else:
#         raise HTTPException(status_code=400, detail="Please provide either id or name")
    
#     if not existing_rag:
#         raise HTTPException(status_code=404, detail="Rag not found")
    
#     result = rag_names_collection.delete_one({"name": name})
    
#     if result.deleted_count == 0:
#         raise HTTPException(status_code=404, detail="Rag not found in mongodb")
#     if not name:
#         name = existing_rag["name"]
#     result_embed = rag_collection.delete_many({"metadata": name})
    
#     if result_embed.deleted_count == 0:
#         raise HTTPException(status_code=404, detail="No embedded documents found for the given RAG")
    
#     return {"message": "Rag and its embedded documents deleted successfully"}

# @app.delete("/allrag")
# async def delete_all_rags():
#     result = rag_names_collection.delete_many({})
    
#     # if result.deleted_count == 0:
#     #     raise HTTPException(status_code=404, detail="No Rags found")
    
#     result_embed = rag_collection.delete_many({})

#     # if result_embed.deleted_count == 0:
#     #     raise HTTPException(status_code=404, detail="No embedded documents found for the given RAG")
    
#     return {"message": "All Rags and their embedded documents deleted successfully"}
    
@app.post("/rag")
async def rag_system(pdf: UploadFile = File(...)):
    # rag_name = rag_names_collection.find_one({"name": pdf.filename.replace(".pdf", "")})
    # if rag_name:
    #     raise HTTPException(status_code=400, detail=f"Pdf with this name already uploaded on {rag_name['created_at']} GMT")
    # else:
    # Create the temp directory if it doesn't exist
    os.makedirs("./temp", exist_ok=True)
    rag_names_collection.insert_one({"name": pdf.filename.replace(".pdf", ""), "created_at": datetime.utcnow()})
    try:
        file_path = f"./temp/{pdf.filename}"
        with open(file_path, "wb") as f:
            f.write(pdf.file.read())
        
        start_time = time.time()
        chunks = process_pdf_and_get_chunks(file_path, pdf.filename.replace(".pdf", ""))
        end_time = time.time()
        time_taken = end_time - start_time
        time_taken = round(time_taken, 2)
        print(f"Time taken to process PDF: {time_taken} seconds")

        rag_collection.insert_many(chunks)
        os.remove(file_path)
        return {"message": f"PDF uploaded and processed successfully in {time_taken} seconds"}
    except Exception as e:
        print(f"Error processing PDF: {e}")
        raise HTTPException(status_code=500, detail="PDF processing failed")

def process_pdf_and_get_chunks(file_path: str, pdf: str):
    print(f"Processing PDF with PyPDFLoader: {file_path}")
    try:
        # Load PDF using PyMuPDF
        pdf_stream = BytesIO(open(file_path, "rb").read())
        doc = fitz.open(stream=pdf_stream, filetype="pdf")
        text = "\n".join([page.get_text("text") for page in doc])
        doc.close()

        if not text:
            raise HTTPException(status_code=400, detail="No text found in PDF.")
        
        # Split the text into chunks
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=150,
            length_function=len,
        )
        chunked_text = text_splitter.split_text(text)
        
        chunks_with_metadata = []
        for chunk in chunked_text:
            chunk_data = {
                "text": chunk,
                "embedding": get_embedding(chunk),
                "metadata": pdf
            }
            chunks_with_metadata.append(chunk_data)
        
        print(f"Successfully split PDF into {len(chunks_with_metadata)} chunks.")
        return chunks_with_metadata
    except Exception as e:
        print(f"An error occurred during PDF processing: {e}")
        # Raise HTTPException to return a proper API error response
        raise HTTPException(status_code=500, detail=f"Failed to process PDF: {e}")

@app.post("/ask_rag")
async def ask_rag(question: str, chat_history: list[dict[str, str]] = []):
    """
    Performs a RAG query against indexed PDF data in MongoDB, includes chat history,
    and asks OpenAI.
    """
    query_text = question
    print(f"Received query: '{query_text}'")

    if not query_text:
         raise HTTPException(status_code=400, detail="Question is required.")

    try:
        # 1. Get embedding for the user query
        start_time_embedding = time.time()
        query_embedding = get_embedding(query_text)
        time_taken_embedding = round(time.time() - start_time_embedding, 2)
        print(f"Time taken to embed query: {time_taken_embedding} seconds")

        if not query_embedding:
             raise HTTPException(status_code=500, detail="Failed to generate embedding for the query.")

        # 2. Search MongoDB for relevant chunks using vector search
        start_time_search = time.time()
        search_results = list(rag_collection.aggregate([
            {
                "$vectorSearch": {
                    "index": "rag_index",
                    "queryVector": query_embedding,
                    "path": "embedding",
                    "numCandidates": 1000,
                    "limit": 10,
                    "metric": "cosine"
                }
            },
            {
                 "$project": {
                    "_id": 0,
                    "text": 1,
                    "score": { "$meta": "vectorSearchScore" }
                 }
            }
        ]))
        time_taken_search = round(time.time() - start_time_search, 2)
        print(f"Time taken for vector search: {time_taken_search} seconds. Found {len(search_results)} results.")

        # 3. Format the retrieved chunks as context
        context = "\n\n---\n\n".join([doc["text"] for doc in search_results])

        # Handle case where PDF doesn't exist or no relevant chunks found for current query
        if not search_results:
            if not chat_history:
                  # No search results and no history, return specific message
                  return {"answer": f"Could not find relevant information for the question based on the content in our database. Please try rephrasing your question."}
             # If search results are empty but there IS chat history, we still proceed
            print("No new relevant chunks found, relying on history and prompt.")


        # 4. Prepare messages for OpenAI, including history and context
        messages = [
            {
                "role": "system",
                "content": (
                    "Dont metion anything about being looking at a provided context. "
                    "You are a helpful assistant that answers questions based on the provided context. "
                    "You can use external knowledge if needed but with a focus on the provided context. "
                    # "Use the chat history to understand the conversation flow and user intent. "
                    # "If the answer cannot be found in the *provided context* and the history doesn't provide enough information, respond with 'I am sorry, but the information needed to answer this question is not available in the provided document or previous conversation context.' "
                    # "Do not use external knowledge. Keep the answer concise and directly address the user's question."
                )
            }
        ]

        # Add previous chat history to the messages list
        messages.extend(chat_history)

        # Add the current user query, incorporating the retrieved context
        current_user_message_content = f"Context:\n{context}\n\nQuestion: {query_text}"

        messages.append({
            "role": "user",
            "content": current_user_message_content
        })

        print(f"Sending {len(messages)} messages to OpenAI API.")
        # print("Messages structure:", messages) # Uncomment to debug the full message structure

        # 5. Call OpenAI API
        start_time_openai = time.time()
        openai.api_key = os.getenv('OPEN_AI')
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.1,
            max_tokens = 384
        )
        time_taken_openai = round(time.time() - start_time_openai, 2)
        print(f"Time taken for OpenAI API call: {time_taken_openai} seconds")

        # 6. Extract the answer
        answer = response.choices[0].message.content.strip()
        print("Answer:", answer)
        # 7. Return the answer
        # The client is responsible for adding the current query and this answer to its history
        return {"answer": answer}

    except HTTPException as e:
        # Re-raise HTTPExceptions
        raise e
    except Exception as e:
        print(f"An error occurred during RAG query with history: {e}")
        raise HTTPException(status_code=500, detail=f"An internal error occurred: {e}")

################### resume parse #######################
import json
from typing import List, Optional
from pydantic import BaseModel

class EducationItem(BaseModel):
    degree: str
    school: str
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    fieldOfStudy: Optional[str] = None

class ExperienceItem(BaseModel):
    title: str
    company: str
    startDate: Optional[str] = None
    endDate: Optional[str] = None

class CVData(BaseModel):
    about: str
    skills: List[str]
    education: List[EducationItem]
    experience: List[ExperienceItem]


def get_embedd_cv_extract(cv, user_id,cv_url):
    print("embedd dats ",cv, user_id,cv_url)
    embed= get_embedding(cv)
    users_collection.update_one(
         {"user_id": user_id},
         {"$set": {"embedding": embed, "cv_url": cv_url}}  
    )
    
    
    
@app.get("/extract-cv-data/")
async def extract_cv_data(cv_url: str , user_id: int ,background_tasks: BackgroundTasks): # 
    print ("cv_url",cv_url,"user_id",user_id)
    try:
        public_id = cv_url.split("/")[-1].replace(".pdf", "")
        text = extract_text_from_pdf_cloud(public_id)
        print("Extracted text:", text)
        print("public_id:",public_id)

        prompt = f"""
You are an intelligent CV parser. From the following resume text, return a valid JSON object with the following keys:

1. "Summary": Generate a professional 5–7 midium length sentence summary based on the full CV content. This must be an original synthesis, not copied from the CV. Clearly identify the candidate's job specialization or target role based on their skills and experience (e.g., "Machine Learning Engineer" or "Full-Stack Developer"). Also include a brief summary of the types of projects they have worked on, highlighting key technologies or outcomes.
2. "About": Extract the **first personal paragraph or section** that appears after the contact information (name, email, phone, location). This is typically an unlabeled personal introduction, profile, or "About Me" paragraph.
3. "Skills": A list of technical and soft skills.
4. "Education": A list of objects with: degree, school, startDate, endDate, fieldOfStudy.
5. "Experience": A list of objects with: title, company, startDate, endDate.

Return only valid JSON, no markdown or explanation.

CV Text:
{text[:3000]}
"""
        print ("before chatgpt api ")  
        start_time = time.time()
        openai.api_key = os.getenv('OPEN_AI')
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1000
        )
        duration = round(time.time() - start_time, 2)
        print(f"✅ OpenAI API call succeeded in {duration} seconds")
 
        result = response.choices[0].message.content.strip()
        # print("🧠 Raw OpenAI Response:\n", result)

        # Parse JSON from response
        json_start = result.find('{')
        json_str = result[json_start:].split("```")[0]
        parsed = json.loads(json_str)
        print("🧠 Parsed OpenAI Response:\n", parsed)
        
        # Normalize missing fields
        parsed.setdefault("About", "")
        parsed.setdefault("Summary", "")
        parsed.setdefault("Skills", [])
        parsed.setdefault("Education", [])
        parsed.setdefault("Experience", [])

        # Normalize skills list
        parsed["Skills"] = [s.strip() for s in parsed["Skills"] if isinstance(s, str)]

        # Ensure Education and Experience entries have required keys
        for edu in parsed["Education"]:
            edu.setdefault("degree", "")
            edu.setdefault("school", "")
            edu.setdefault("startDate", "")
            edu.setdefault("endDate", "")
            edu.setdefault("fieldOfStudy", "")
        
        for exp in parsed["Experience"]:
            if isinstance(exp, dict):
                exp.setdefault("title", "")
                exp.setdefault("company", "")
                exp.setdefault("startDate", "")
                exp.setdefault("endDate", "")
            else:
                # fallback in case it is just a string
                parsed["Experience"] = [{
                    "title": exp,
                    "company": "",
                    "startDate": "",
                    "endDate": ""
                }]        
                # } for exp in parsed["Experience"]]
                break
        background_tasks.add_task(get_embedd_cv_extract,text,user_id,public_id)
        return parsed

    except Exception as e:
        print("❌ GPT API failed:", e)
        raise HTTPException(status_code=500, detail=f"AI parsing failed: {e}")
######### summary generation ##############





@app.get("/top_talents")
async def top_talents(job_id: int, page: int = 1, page_size: int = 5):
    try:
        if not job_id:
            raise HTTPException(status_code=400, detail="Job ID is required")
        if page < 1 or page_size < 1:
            raise HTTPException(status_code=400, detail="Page numbers must be 1 or higher")

        page_count= users_collection.count_documents({'embedding': {'$exists': True}})

        if page_count == 0:
            raise HTTPException(status_code=404, detail="No users found")
        if page*page_size > 100:
            raise HTTPException(status_code=400, detail="Max recommendations is 100")
        if page > page_count/page_size and page > 1:
            raise HTTPException(status_code=400, detail="Page number exceeds available pages")

        job = jobs_collection.find_one({"id": job_id})
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        combined_embedding = job["combined_embedding"]
        if not combined_embedding:
            raise HTTPException(status_code=500, detail="Combined embedding not found for the job")

        # Get top talents
        skip = (page - 1) * page_size
        results = list(users_collection.aggregate([
            {
                "$vectorSearch": {
                    "index": "default",
                    "queryVector": job["combined_embedding"],
                    "path": "embedding",
                    "numCandidates": 1000,
                    "limit": 100,
                    "metric": "cosine"
                }
            },
            {
                 "$project": {
                    "_id": 0,
                    "id": '$user_id',
                    "ats_res": { "$meta": "vectorSearchScore" },
                    "name": 1,
                    "email": 1
                 }
            },
            {"$skip": skip},  # Skip previous pages
            {"$limit": page_size}  #  Limit results per page
        ]))

        if not results or len(results) == 0:
            raise HTTPException(status_code=404, detail="No matching talents found")
        # print(results)
        return {
            "count": page_count if page_count < 100 else 100,
            "results": results
        }
    except Exception as e:
        print(f"An error occurred while retrieving the job: {e}")
        raise HTTPException(status_code=500, detail=f"An internal error occurred: {e}")

