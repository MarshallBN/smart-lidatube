import requests


class LidarrClient:
    def __init__(self,base_url,api_key,session=requests,timeout=30): self.base=base_url.rstrip("/"); self.session=session; self.timeout=timeout; self.headers={"X-Api-Key":api_key}
    def get_track(self,track_id):
        r=self.session.get(f"{self.base}/api/v1/track/{track_id}",headers=self.headers,timeout=self.timeout); r.raise_for_status(); return r.json()
    def get_track_file(self,file_id):
        r=self.session.get(f"{self.base}/api/v1/trackfile/{file_id}",headers=self.headers,timeout=self.timeout); r.raise_for_status(); return r.json()
    def delete_track_file(self,file_id):
        r=self.session.delete(f"{self.base}/api/v1/trackfile/{file_id}",headers=self.headers,timeout=self.timeout); r.raise_for_status()
    def manual_import(self,path,track):
        body={"id":track["id"],"path":path,"name":track["title"],"artistId":track["artistId"],"albumId":track["albumId"],"albumReleaseId":track["albumReleaseId"],"quality":{},"releaseGroup":"","indexerFlags":0,"downloadId":"","additionalFile":False,"replaceExistingFiles":True,"disableReleaseSwitching":False,"rejections":[]}
        r=self.session.post(f"{self.base}/api/v1/manualimport",json=[body],headers={**self.headers,"Content-Type":"application/json"},timeout=self.timeout); r.raise_for_status(); return r.json() if hasattr(r,"json") else None


class NavidromeClient:
    def __init__(self,base_url,user,password,get=requests.get,timeout=15): self.base=base_url.rstrip("/"); self.get=get; self.timeout=timeout; self.auth={"u":user,"p":password,"v":"1.16.1","c":"smart-lidatube","f":"json"}
    def _call(self,name,**params):
        r=self.get(f"{self.base}/{name}.view",params={**self.auth,**params},timeout=self.timeout); r.raise_for_status(); return r.json()["subsonic-response"]
    def retry_entries(self):
        playlists=self._call("getPlaylists").get("playlists",{}).get("playlist",[]); out=[]
        for p in playlists:
            if p["name"] not in ("Retry","Manual Retry"): continue
            entries=self._call("getPlaylist",id=p["id"]).get("playlist",{}).get("entry",[])
            out.extend({**song,"playlist_id":p["id"],"playlist_name":p["name"],"playlist_index":i} for i,song in enumerate(entries))
        return out
    def remove_entry(self,playlist_id,index): self._call("updatePlaylist",playlistId=playlist_id,songIndexToRemove=index)
