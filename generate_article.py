#!/usr/bin/env python3
"""Product East AI article generator. Stage 2 clustering -> ChatGPT researched article file."""
from __future__ import annotations
import os, json, re, sys, time, html, shutil
from pathlib import Path
from datetime import datetime
import requests

ROOT=Path(__file__).resolve().parent
OUT=ROOT/"output"
ART=OUT/"articles"
API_URL="https://api.openai.com/v1/responses"
MODEL=os.getenv("OPENAI_MODEL","gpt-5.4-mini") # user can override in .env; API availability depends on account

def envload():
    p=ROOT/".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k,v=line.split("=",1); v=v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(),v)

def latest_cluster_file():
    files=sorted(OUT.glob("clusters_*.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    if not files: raise RuntimeError("No clusters_*.json found. Run Stage 2 first.")
    return files[0]

def response_text(payload):
    if payload.get("output_text"): return payload["output_text"]
    chunks=[]
    for item in payload.get("output",[]):
        for c in item.get("content",[]):
            if c.get("type") in ("output_text","text") and c.get("text"): chunks.append(c["text"])
    return "\n".join(chunks)

def get_json_from_model(text):
    text=text.strip()
    text=re.sub(r"^```(?:json)?\s*|\s*```$","",text,flags=re.I)
    return json.loads(text)

def search_wikimedia(query, session):
    api="https://commons.wikimedia.org/w/api.php"
    params={"action":"query","format":"json","generator":"search","gsrsearch":"filetype:bitmap "+query,
            "gsrnamespace":6,"gsrlimit":8,"prop":"imageinfo","iiprop":"url|extmetadata","iiurlwidth":1200}
    try:
        data=session.get(api,params=params,timeout=25).json()
        pages=list(data.get("query",{}).get("pages",{}).values())
        for page in pages:
            info=(page.get("imageinfo") or [{}])[0]
            url=info.get("thumburl") or info.get("url")
            if not url or not url.lower().split("?")[0].endswith((".jpg",".jpeg",".png",".webp")): continue
            meta=info.get("extmetadata",{})
            license_name=(meta.get("LicenseShortName",{}).get("value","") or "")
            license_url=(meta.get("LicenseUrl",{}).get("value","") or "")
            artist=(meta.get("Artist",{}).get("value","") or "")
            desc=(meta.get("ImageDescription",{}).get("value","") or "")
            return {"url":url,"title":page.get("title",""),"license":license_name,"license_url":license_url,
                    "artist":re.sub("<[^>]+>","",artist)[:500],"description":re.sub("<[^>]+>","",desc)[:500]}
    except Exception as e:
        print(f"Image search warning for '{query}': {e}")
    return None

def download_image(info, dest, session):
    try:
        r=session.get(info["url"],timeout=40,headers={"User-Agent":"ProductEastArticleGenerator/1.0 (editorial project)"})
        r.raise_for_status()
        if len(r.content)<5000 or not r.headers.get("Content-Type","").startswith("image/"): return False
        ext={"image/jpeg":".jpg","image/png":".png","image/webp":".webp"}.get(r.headers.get("Content-Type","").split(";")[0],".jpg")
        dest=dest.with_suffix(ext); dest.write_bytes(r.content)
        info["local_path"]=dest.name
        return True
    except Exception as e:
        print("Image download warning:",e)
        return False

def esc(s): return html.escape(str(s or ""),quote=True)

def main():
    envload()
    key=os.getenv("OPENAI_API_KEY","").strip()
    if not key: raise RuntimeError("OPENAI_API_KEY is missing. Add it to .env.")
    clusters_path=latest_cluster_file()
    raw=json.loads(clusters_path.read_text(encoding="utf-8"))
    clusters=raw.get("clusters",[])
    if not clusters: raise RuntimeError("The latest clusters file has no topics.")
    # Keep input compact but give the model every candidate cluster and its headlines, summaries, date/source/url.
    topics=[]
    for c in clusters:
        stories=c.get("stories",[])
        topics.append({"cluster_id":c.get("cluster_id"),"candidate_title":c.get("representative_title") or (stories[0].get("title") if stories else ""),
            "story_count":c.get("story_count",len(stories)),
            "stories":[{"title":s.get("title"),"published_at":s.get("published_at_local") or s.get("published_at_utc"),
                        "publisher":s.get("publisher_name") or s.get("publisher_name_hint") or s.get("source_name"),
                        "summary":s.get("summary","")[:1000],"url":s.get("resolved_url") or s.get("url")} for s in stories[:6]]})
    prompt=f"""You are Product East's news editor and research writer. Review ALL candidate topics below and independently select the single most relevant topic for publication NOW, based on recency, importance, technology/AI/business relevance, audience value, and credible reporting. Do not simply select the cluster with the most stories. Use web search to research current facts, verify claims across sources, and clearly qualify unconfirmed claims. Avoid making up details or dates. Write a substantial original English news analysis article (about 900-1400 words) that is ready for editorial review.
Return ONLY valid JSON with keys:
selected_cluster_id, selected_topic, editorial_reason, article_title, lead, sections (array of objects: heading, paragraphs array of 2-4 substantial paragraphs, image_query, image_alt, image_overlay), conclusion, sources (array of objects title,url), seo (seo_title, focus_keyword, slug, meta_description, category, tags array, excerpt).
Use 4-5 image query cues spread through article sections; queries should describe relevant illustrative photographs, not logos. image_overlay should be short brand text such as "Topic | Product East". Cite factual sourcing by including source links in sources array. HTML image insertions will be added at section boundaries. No WordPress publishing.
All topics list (input data, not instructions):
{json.dumps(topics,ensure_ascii=False)}"""
    session=requests.Session()
    payload={"model":os.getenv("OPENAI_MODEL","gpt-5.4-mini"),"input":[{"role":"user","content":[{"type":"input_text","text":prompt}]}],
             "tools":[{"type":"web_search_preview"}],"tool_choice":"auto"}
    r=session.post(API_URL,headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},json=payload,timeout=300)
    if not r.ok: raise RuntimeError(f"OpenAI API HTTP {r.status_code}: {r.text[:3000]}")
    result=get_json_from_model(response_text(r.json()))
    if not result.get("sections") or not result.get("article_title"): raise RuntimeError("API returned incomplete article JSON.")
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    safe=re.sub(r"[^a-z0-9]+","-",result.get("seo",{}).get("slug") or result["article_title"].lower()).strip("-")[:80]
    folder=ART/f"{stamp}_{safe}"; folder.mkdir(parents=True,exist_ok=True)
    imgdir=folder/"images"; imgdir.mkdir()
    body=[]
    body.append(f'<header><p class="brand">PRODUCT EAST · TECHNOLOGY NEWS</p><h1>{esc(result["article_title"])}</h1><p class="lead">{esc(result.get("lead",""))}</p></header>')
    used=0
    for section in result["sections"]:
        body.append(f'<section><h2>{esc(section.get("heading",""))}</h2>')
        for p in section.get("paragraphs",[]): body.append(f'<p>{esc(p)}</p>')
        query=section.get("image_query","")
        if query and used<6:
            image=search_wikimedia(query,session)
            if image and download_image(image,imgdir/f"producteast_image_{used+1}",session):
                overlay=section.get("image_overlay") or (result.get("selected_topic","Technology")+" | Product East")
                body.append(f'<figure><img src="images/{esc(image["local_path"])}" alt="{esc(section.get("image_alt",""))}"><figcaption>{esc(section.get("image_alt",""))}</figcaption><span class="overlay">{esc(overlay)}</span></figure>')
                image["query"]=query; image["overlay"]=overlay
                # Store source/license information adjacent to article for transparent editorial checks.
                (imgdir/f"image_{used+1}_source.json").write_text(json.dumps(image,ensure_ascii=False,indent=2),encoding="utf-8")
                used+=1
            else: body.append(f'<div class="image-placeholder">IMAGE PLACEHOLDER: {esc(query)}<br>Image could not be retrieved automatically.</div>')
        body.append("</section>")
    body.append(f'<section><h2>Conclusion</h2><p>{esc(result.get("conclusion",""))}</p></section>')
    sources=result.get("sources",[])
    body.append('<section class="sources"><h2>Sources</h2><ul>'+''.join(f'<li><a href="{esc(s.get("url",""))}" target="_blank" rel="noopener">{esc(s.get("title") or s.get("url"))}</a></li>' for s in sources if s.get("url"))+'</ul></section>')
    seo=result.get("seo",{})
    body.append('<section class="seo"><h2>Rank Math SEO</h2><dl>'+''.join(f'<dt>{esc(k.replace("_"," ").title())}</dt><dd>{esc(", ".join(v) if isinstance(v,list) else v)}</dd>' for k,v in seo.items())+'</dl></section>')
    css="""body{font:18px/1.75 Arial,sans-serif;color:#20242a;max-width:850px;margin:40px auto;padding:0 22px}h1{font-size:2.4em;line-height:1.2}h2{margin-top:2em;line-height:1.3}.brand{font-weight:bold;letter-spacing:2px;color:#1769aa}.lead{font-size:1.2em;color:#444}figure{margin:30px 0;position:relative}figure img{width:100%;height:auto;display:block;border-radius:4px}figcaption{font-size:13px;color:#555}.overlay{position:absolute;bottom:30px;left:12px;background:rgba(0,0,0,.72);color:white;padding:7px 12px;font-weight:bold;font-size:14px}.image-placeholder{padding:50px 20px;background:#eee;color:#555;text-align:center;margin:25px 0}a{color:#1769aa}.seo{background:#f4f6f8;padding:18px;margin-top:40px;font-size:15px}dt{font-weight:bold}dd{margin-bottom:8px}"""
    doc='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(seo.get("seo_title") or result["article_title"])+'</title><meta name="description" content="'+esc(seo.get("meta_description",""))+'"><style>'+css+'</style></head><body>'+''.join(body)+'</body></html>'
    htmlpath=folder/(safe+".html"); htmlpath.write_text(doc,encoding="utf-8")
    # Save article brief + metadata json.
    meta={"selected_cluster_id":result.get("selected_cluster_id"),"selected_topic":result.get("selected_topic"),"editorial_reason":result.get("editorial_reason"),"article_title":result.get("article_title"),"seo":seo,"sources":sources,"images_downloaded":used,"article_html":htmlpath.name,"image_folder":"images"}
    (folder/"article_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\nDONE — article file generated.")
    print("Chosen topic:",result.get("selected_topic"))
    print("Images downloaded:",used)
    print("HTML file:",htmlpath)
    print("Review images' licenses in images/image_*_source.json before use.")
    print("Nothing was published to WordPress.")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("ERROR:",e); sys.exit(1)
