FROM python@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e

WORKDIR /adapter

COPY mock_service_adapter.py /adapter/mock_service_adapter.py

ENTRYPOINT ["python3", "/adapter/mock_service_adapter.py"]
