## Docker/containerd storage migration 

```bash
sudo python3 script/migrate_docker_storage.py migrate
sudo python3 script/migrate_docker_storage.py verify
sudo python3 script/migrate_docker_storage.py restore
sudo python3 script/migrate_docker_storage.py delete-old-data \
  --confirm DELETE-OLD-DOCKER-DATA
```
