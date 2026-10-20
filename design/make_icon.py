# Renders SkyCam/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png:
#   ~/ASICAP/.venv/bin/python3 design/make_icon.py SkyCam/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png
# Night sky, a camera lens with an Andromeda-like galaxy in the glass, red night-mode dot on the ring.
import numpy as np, cv2, math, sys
SS=4; N=1024*SS; rng=np.random.default_rng(31)
yy,xx=np.mgrid[0:N,0:N].astype(np.float32)/N
# background: deep indigo glow upper-left fading to near-black
r=np.hypot(xx-0.38,yy-0.30)
t=np.clip(r/0.95,0,1)
top=np.array([74,38,22],np.float32)      # BGR  (#16264a)
bot=np.array([14,8,4],np.float32)        # BGR  (#04080e)
img=(top*(1-t[...,None])**1.4+bot*(1-(1-t[...,None])**1.4))
# faint Milky-Way band
band=np.exp(-((yy-0.95*xx-0.05)/0.16)**2)*0.35
img+=band[...,None]*np.array([40,30,26],np.float32)
# stars
def star(img,x,y,rad,b,col):
    s=int(rad*6); x0,y0=int(x),int(y)
    ys,xs=np.mgrid[max(0,y0-s):min(N,y0+s),max(0,x0-s):min(N,x0+s)].astype(np.float32)
    g=np.exp(-((xs-x)**2+(ys-y)**2)/(2*rad**2))*b
    img[max(0,y0-s):min(N,y0+s),max(0,x0-s):min(N,x0+s)]+=g[...,None]*np.array(col,np.float32)
for _ in range(170):
    x,y=rng.uniform(0,N,2); b=rng.uniform(40,200)*(rng.random()**2.5+0.15); rad=rng.uniform(1.2,2.6)*SS
    col=[(255,235,220),(220,235,255),(255,255,255)][rng.integers(3)]
    if math.hypot(x/N-0.5,y/N-0.53)<0.36: b*=0.25           # keep the lens area cleaner
    star(img,x,y,rad,b/255*1.0,col)
for (x,y,b) in ((0.16,0.18,1.0),(0.83,0.13,0.9),(0.12,0.82,0.7),(0.88,0.86,0.8)):
    star(img,x*N,y*N,3.2*SS,0.9*b,(255,245,235)); star(img,x*N,y*N,9*SS,0.18*b,(255,230,200))
img=np.clip(img,0,255)
cx,cy=0.5*N,0.535*N; R1=0.335*N; R2=0.265*N
d=np.hypot(xx*N-cx,yy*N-cy)
# lens barrel ring: brushed metal with a top-left highlight
ring=(d<R1)&(d>R2)
ang=np.arctan2(yy*N-cy,xx*N-cx)
metal=110+70*np.cos(ang+2.4)+25*np.cos(6*ang)*0+np.clip((R1-d)/(R1-R2),0,1)*20
ringcol=np.stack([metal*1.02,metal*0.98,metal*0.95],-1)
aa=lambda edge,w=1.5*SS: np.clip((edge)/w+0.5,0,1)
ring_a=aa(R1-d)*aa(d-R2)
img=img*(1-ring_a[...,None])+ringcol*ring_a[...,None]
# thin inner bevel
bev=np.exp(-((d-R2)/(2.0*SS))**2)*0.9
img=img*(1-bev[...,None])+np.array([40,30,25],np.float32)*bev[...,None]
# lens glass: dark with a deep blue sheen
inside=aa(R2-d)
glass=np.stack([np.full_like(d,26),np.full_like(d,14),np.full_like(d,8)],-1)+ (np.clip(1-d/R2,0,1)**1.5)[...,None]*np.array([60,30,18],np.float32)
img=img*(1-inside[...,None])+glass*inside[...,None]
# galaxy inside the lens (M31-like): tilted ellipse, warm core, bluish disk
gx,gy=cx+0.0*N,cy+0.01*N; th=math.radians(-28)
X=(xx*N-gx)*math.cos(th)+(yy*N-gy)*math.sin(th); Y=-(xx*N-gx)*math.sin(th)+(yy*N-gy)*math.cos(th)
disk=np.exp(-((X/(0.19*N))**2+(Y/(0.055*N))**2))
halo=np.exp(-((X/(0.24*N))**2+(Y/(0.085*N))**2))*0.35
core=np.exp(-((X/(0.028*N))**2+(Y/(0.02*N))**2))
lane=1-0.45*np.exp(-((Y-0.018*N)/(0.007*N))**2)*np.exp(-(X/(0.17*N))**2)
gal=(disk*0.85+halo)*lane
glow=gal[...,None]*np.array([255,215,185],np.float32)*0.85+core[...,None]*np.array([150,215,255],np.float32)*1.35
img=img+glow*inside[...,None]
# a few stars inside the lens
for (x,y,b) in ((0.40,0.43,0.8),(0.62,0.62,0.6),(0.57,0.41,0.5),(0.42,0.66,0.45)):
    star(img,x*N,y*N,2.2*SS,b*inside[int(y*N),int(x*N)],(255,250,245))
# glass reflection arc top-left
refl=np.exp(-((d-R2*0.82)/(0.012*N))**2)*np.clip(np.cos(ang+2.35),0,1)**6*inside*0.35
img=img+refl[...,None]*255
# red night-mode dot (record light) on the ring, top-right
rx,ry=cx+math.cos(math.radians(-45))*(R1+R2)/2, cy+math.sin(math.radians(-45))*(R1+R2)/2
dd=np.hypot(xx*N-rx,yy*N-ry); dot=aa(0.03*N-dd)
img=img*(1-dot[...,None])+np.array([40,40,235],np.float32)*dot[...,None]
img+= (np.exp(-(dd/(0.05*N))**2)*0.35)[...,None]*np.array([30,30,200],np.float32)
img=np.clip(img,0,255).astype(np.uint8)
out=cv2.resize(img,(1024,1024),interpolation=cv2.INTER_AREA)
cv2.imwrite(sys.argv[1],out)
