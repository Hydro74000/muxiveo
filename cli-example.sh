python3 main.py --cli batch \
  --template "/chemin/vers/serie1.S01E01.4KDV.exact-job.json" \
  --input-dir "/chemin/vers/sources/serie1.S*E*.*.2160p.*" \
  --output-dir "/chemin/vers/sortie/serie1-4KDV" \
  --auto-tmdb \
  --output-template "{title:release}.{season_episode}.{episode_title:release}.MULTi.VF2.HDR.DV.2160p.WEB.H265-{group}-MVO"

