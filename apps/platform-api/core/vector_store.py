import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5


class VectorStoreError(Exception):
    pass


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class QdrantVectorStore:
    def __init__(self, base_url=None, collection=None, api_key_ref=None, timeout_seconds=15):
        self.base_url = (base_url or os.getenv("QDRANT_URL", "")).rstrip("/")
        self.collection = collection or os.getenv("QDRANT_COLLECTION", "agent_knowledge")
        self.timeout_seconds = timeout_seconds
        if not self.base_url.startswith("https://") and not self.base_url.startswith("http://"):
            raise VectorStoreError("Qdrant URL must use HTTP or HTTPS.")
        if not self.collection or len(self.collection) > 128:
            raise VectorStoreError("Qdrant collection name is invalid.")
        reference = api_key_ref or os.getenv("QDRANT_API_KEY_REF", "")
        self.api_key = self._resolve_api_key(reference)

    @staticmethod
    def _resolve_api_key(reference):
        if not reference:
            return ""
        if not reference.startswith("env:"):
            raise VectorStoreError("Qdrant API key must use an env: reference.")
        value = os.getenv(reference[4:])
        if not value:
            raise VectorStoreError("Qdrant API key is not configured in the runtime.")
        return value

    def _request(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["api-key"] = self.api_key
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with build_opener(NoRedirectHandler()).open(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            raise VectorStoreError(f"Vector database returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VectorStoreError("Vector database is unavailable.") from exc
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise VectorStoreError("Vector database returned invalid JSON.") from exc
        if not isinstance(payload, dict) or payload.get("status") == "error":
            raise VectorStoreError("Vector database operation failed.")
        return payload

    @staticmethod
    def _validate_vector(vector):
        if not isinstance(vector, list) or not vector:
            raise VectorStoreError("Embedding vector must be a non-empty array.")
        values = [float(value) for value in vector]
        if any(not math.isfinite(value) for value in values):
            raise VectorStoreError("Embedding vector contains non-finite values.")
        return values

    def ensure_collection(self, vector_size, distance="Cosine"):
        if not isinstance(vector_size, int) or vector_size <= 0:
            raise VectorStoreError("Vector size must be a positive integer.")
        if distance not in {"Cosine", "Dot", "Euclid"}:
            raise VectorStoreError("Unsupported vector distance.")
        self._request(
            "PUT",
            f"/collections/{self.collection}",
            {"vectors": {"size": vector_size, "distance": distance}},
        )

    def upsert(self, tenant_id, project_id, records):
        if not tenant_id or not project_id or not isinstance(records, list) or not records:
            raise VectorStoreError("Tenant, project and at least one record are required.")
        points = []
        for record in records:
            text = record.get("text")
            chunk_id = str(record.get("chunk_id") or "")
            if not isinstance(text, str) or not text.strip() or not chunk_id:
                raise VectorStoreError("Each vector record requires text and chunk_id.")
            vector = self._validate_vector(record.get("vector"))
            point_id = str(uuid5(NAMESPACE_URL, f"{tenant_id}:{project_id}:{chunk_id}"))
            payload = {
                "tenant_id": str(tenant_id),
                "project_id": str(project_id),
                "chunk_id": chunk_id,
                "text": text,
                "metadata": record.get("metadata") or {},
            }
            points.append({"id": point_id, "vector": vector, "payload": payload})
        self._request(
            "PUT",
            f"/collections/{self.collection}/points?wait=true",
            {"points": points},
        )
        return {"upserted": len(points)}

    def delete_document(self, tenant_id, project_id, document_id):
        if not tenant_id or not project_id or not document_id:
            raise VectorStoreError("Tenant, project and document scope are mandatory.")
        self._request(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {
                "filter": {
                    "must": [
                        {"key": "tenant_id", "match": {"value": str(tenant_id)}},
                        {"key": "project_id", "match": {"value": str(project_id)}},
                        {
                            "key": "metadata.document_id",
                            "match": {"value": str(document_id)},
                        },
                    ]
                }
            },
        )
        return {"deleted": True}

    def search(
        self,
        tenant_id,
        project_id,
        vector,
        limit=5,
        score_threshold=None,
        metadata_filters=None,
    ):
        if not tenant_id or not project_id:
            raise VectorStoreError("Tenant and project scope are mandatory for vector search.")
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise VectorStoreError("Search limit must be between 1 and 50.")
        must_filters = [
            {"key": "tenant_id", "match": {"value": str(tenant_id)}},
            {"key": "project_id", "match": {"value": str(project_id)}},
        ]
        if metadata_filters is not None:
            if not isinstance(metadata_filters, dict) or len(metadata_filters) > 8:
                raise VectorStoreError("Metadata filters must be an object with at most 8 entries.")
            for key, value in metadata_filters.items():
                if (
                    not isinstance(key, str)
                    or not key.replace("_", "").isalnum()
                    or not key
                    or value is None
                    or isinstance(value, (dict, list))
                ):
                    raise VectorStoreError("Metadata filter key or value is invalid.")
                must_filters.append(
                    {
                        "key": f"metadata.{key}",
                        "match": {"value": value},
                    }
                )
        body = {
            "vector": self._validate_vector(vector),
            "limit": limit,
            "with_payload": True,
            "filter": {"must": must_filters},
        }
        if score_threshold is not None:
            threshold = float(score_threshold)
            if not 0 <= threshold <= 1:
                raise VectorStoreError("Score threshold must be between 0 and 1.")
            body["score_threshold"] = threshold
        payload = self._request(
            "POST",
            f"/collections/{self.collection}/points/search",
            body,
        )
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise VectorStoreError("Vector database returned an invalid search result.")
        return [
            {
                "id": item.get("id"),
                "score": item.get("score"),
                "payload": item.get("payload") or {},
            }
            for item in result
        ]
