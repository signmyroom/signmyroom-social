"""SignMyRoom reel music generator (all synthesized = royalty-free, safe for paid ads).
Usage: python3 tools/musicgen.py <lofi|ukulele|tropical|funk|folk> <seconds> out.wav [--transpose N] [--tempo X] [--seed N]
Then mux: measure ebur128, volume to -14 LUFS, alimiter=limit=0.84:level=false, AAC 192k stereo.
Mike approved all 5 styles 2026-09-30."""
import numpy as np, sys, wave
from scipy.signal import butter, sosfilt, fftconvolve
SR=44100
import argparse
ap=argparse.ArgumentParser(description='SignMyRoom royalty-free reel music generator')
ap.add_argument('style',choices=['lofi','ukulele','tropical','funk','folk']);ap.add_argument('dur',type=float);ap.add_argument('out')
ap.add_argument('--transpose',type=int,default=0,help='semitones, -5..+5');ap.add_argument('--tempo',type=float,default=1.0,help='tempo multiplier 0.9..1.1');ap.add_argument('--seed',type=int,default=7)
A=ap.parse_args();TR=A.transpose;BPMX=A.tempo
rng=np.random.default_rng(A.seed)
def midi(n): return 440*2**((n+TR-69)/12)
def env(n,a,r):
    e=np.ones(n); a=max(1,min(a,n//2)); r=max(1,min(r,n-a))
    e[:a]=0.5-0.5*np.cos(np.linspace(0,np.pi,a)); e[n-r:]*=0.5+0.5*np.cos(np.linspace(0,np.pi,r)); return e
def add(buf,t,sig,g=1.0):
    i=int(t*SR)
    if i>=len(buf) or i<0: return
    j=min(len(buf),i+len(sig)); buf[i:j]+=g*sig[:j-i]
def lp(x,f,o=2): return sosfilt(butter(o,f,'low',fs=SR,output='sos'),x)
def hp(x,f,o=2): return sosfilt(butter(o,f,'high',fs=SR,output='sos'),x)
def bp(x,a,b,o=2): return sosfilt(butter(o,[a,b],'bandpass',fs=SR,output='sos'),x)
T=lambda d: np.arange(int(d*SR))/SR
# instruments
def rhodes(f,d):
    t=T(d); s=np.sin(2*np.pi*f*t+0.6*np.sin(2*np.pi*f*t)*np.exp(-t*6))*np.exp(-t*1.2)+0.25*np.sin(2*np.pi*2*f*t)*np.exp(-t*5)
    return s*(1+0.08*np.sin(2*np.pi*4.5*t))*env(len(t),200,4000)
def ks(f,d,damp=0.996,soft=4):
    n=int(d*SR); p=max(2,int(SR/f)); b=np.convolve(rng.uniform(-1,1,p),np.ones(soft)/soft,'same'); out=np.zeros(n)
    for i in range(n):
        out[i]=b[i%p]; b[i%p]=damp*0.5*(b[i%p]+b[(i+1)%p])
    return out*env(n,40,3000)
def pluck(f,d,bright=6):
    t=T(d); s=np.zeros(len(t))
    for h in range(1,9): s+=((-1)**(h+1))/h*np.sin(2*np.pi*f*h*t)*np.exp(-t*(3+h*bright*0.5))
    return s*env(len(t),60,3000)
def pad(fs,d):
    t=T(d); s=np.zeros(len(t))
    for f in fs:
        for dt in (-0.002,0,0.002):
            for h,a in ((1,1),(2,.3),(3,.12)): s+=a*np.sin(2*np.pi*f*h*(1+dt)*t)
    return s/len(fs)/4*env(len(t),int(.3*SR),int(.4*SR))
def bass(f,d,punch=0):
    t=T(d); ff=f*(1+punch*np.exp(-t*40)); ph=2*np.pi*np.cumsum(ff)/SR
    return 0.5*(np.sin(ph)+.45*np.sin(2*ph)+.2*np.sin(3*ph))*np.exp(-t*2)*env(len(t),80,2000)
def kick(g=1):
    t=T(.3); f=120*np.exp(-t*25)+45; return 0.45*g*np.sin(2*np.pi*np.cumsum(f)/SR)*np.exp(-t*12)
def snare(g=1,tone=190):
    t=T(.25); n=bp(rng.normal(0,1,len(t)),900,5000)*np.exp(-t*22); b=np.sin(2*np.pi*tone*t)*np.exp(-t*30)
    return g*(0.6*n+0.5*b)
def clap(g=1):
    t=T(.2); n=bp(rng.normal(0,1,len(t)),1000,4000); e=np.zeros(len(t))
    for o in (0,.011,.022): i=int(o*SR); e[i:]+=np.exp(-(t[:len(t)-i])*40)
    return g*n*e/2
def hat(g=1,d=.05):
    t=T(d); return g*bp(rng.normal(0,1,len(t)),6000,9500)*np.exp(-t*80)
def shaker(g=1): 
    t=T(.07); return g*bp(rng.normal(0,1,len(t)),4000,8000)*np.exp(-t*50)*env(len(t),200,200)
def finish(L,R,dur,rev=.2,lpf=10500):
    ir=rng.normal(0,1,int(1.4*SR))*np.exp(-T(1.4)*3.5); ir/=np.abs(ir).sum()/8
    out=[]
    for ch in (L,R):
        x=ch+rev*fftconvolve(ch,ir)[:len(ch)]; x=lp(x,lpf,4); x=hp(x,35); out.append(x)
    x=np.stack(out,1); x/=np.abs(x).max()/0.72; x=np.tanh(x*1.02)/np.tanh(1.02)*0.85
    n=len(x); fo=int(.8*SR); x[n-fo:]*=(np.linspace(1,0,fo)**1.5)[:,None]; fi=int(.05*SR); x[:fi]*=np.linspace(0,1,fi)[:,None]
    return x
def st(L,R,t,s,g=1,pan=0):
    add(L,t,s,g*(1-max(0,pan))); add(R,t,s,g*(1+min(0,pan)))
def song(style,dur):
    n=int((dur+2)*SR); L=np.zeros(n); R=np.zeros(n)
    if style=='lofi':
        bpm=78*BPMX; prog=[(62,[62,65,69,72]),(55,[59,62,65,69]),(60,[60,64,67,71]),(57,[57,60,64,67])] # Dm9 G13ish Cmaj7 Am7
        beat=60/bpm; bar=4*beat
        for b in range(int(dur/bar)+2):
            r,ch=prog[b%4]; t0=b*bar
            for k,nn in enumerate(ch): st(L,R,t0+k*0.03,rhodes(midi(nn),bar*0.95),.35,pan=(k-1.5)*.2)
            st(L,R,t0+2*beat,rhodes(midi(ch[-1]+12),beat*1.5),.18,.3)
            st(L,R,t0,bass(midi(r-24),beat*1.8),.55); st(L,R,t0+2.5*beat,bass(midi(r-24),beat*1.2),.45)
            for k in (0,2.5): st(L,R,t0+k*beat,kick(.8))
            for k in (1,3): st(L,R,t0+k*beat+0.02,snare(.35,180))
            for k in range(8): st(L,R,t0+k*beat/2+(0.03 if k%2 else 0),hat(.12 if k%2 else .18),pan=.3)
        return finish(L[:int(dur*SR)],R[:int(dur*SR)],dur,.25,8000)
    if style=='ukulele':
        bpm=116*BPMX; prog=[[67,72,76,79],[67,71,74,79],[69,72,76,81],[65,69,72,77]] # C G Am F uke voicings
        roots=[48,43,45,41]; beat=60/bpm; bar=4*beat
        pat=[(0,1),(1,1),(1.5,-1),(2.5,-1),(3,1),(3.5,-1)]
        mel=[79,81,79,76, 74,76,79,74, 76,77,76,72, 72,74,77,76]
        for b in range(int(dur/bar)+2):
            ch=prog[b%4]; t0=b*bar
            for pos,dirn in pat:
                notes=ch if dirn>0 else ch[::-1]
                for k,nn in enumerate(notes): st(L,R,t0+pos*beat+k*0.012,ks(midi(nn),beat*1.2,0.994,3),.28,pan=-.25)
            for k in range(4): st(L,R,t0+k*beat+0.01,pluck(midi(mel[(b%4)*4+k]),beat*0.9,4),.22,pan=.3)
            st(L,R,t0,bass(midi(roots[b%4]-12),beat*1.5),.45); st(L,R,t0+2*beat,bass(midi(roots[b%4]-5),beat*1.5),.4)
            for k in (0,2): st(L,R,t0+k*beat,kick(.55))
            for k in (1,3): st(L,R,t0+k*beat,clap(.5))
            for k in range(8): st(L,R,t0+k*beat/2,shaker(.14),pan=.4)
        return finish(L[:int(dur*SR)],R[:int(dur*SR)],dur,.15,11000)
    if style=='tropical':
        bpm=100*BPMX; prog=[(57,[69,72,76]),(53,[69,72,77]),(60,[67,72,76]),(55,[67,71,74])] # Am F C G
        beat=60/bpm; bar=4*beat
        hook=[76,0,79,76,74,0,72,74]
        for b in range(int(dur/bar)+2):
            r,ch=prog[b%4]; t0=b*bar
            p=pad([midi(x) for x in ch],bar); duck=1-0.6*np.exp(-((T(bar)%beat))*9); st(L,R,t0,p*duck,.35)
            for k,nn in enumerate(hook):
                if nn: st(L,R,t0+k*beat/2,pluck(midi(nn),beat*0.8,7),.3,pan=(.3 if k%2 else -.3))
            for k in range(4): st(L,R,t0+k*beat,kick(.75))
            for k in (1,3): st(L,R,t0+k*beat,clap(.35))
            for k in range(4): st(L,R,t0+k*beat+beat/2,hat(.16,.08),pan=.3)
            for k in (0,1.5,3): st(L,R,t0+k*beat,bass(midi(r-24),beat*0.9,0.3),.45)
        return finish(L[:int(dur*SR)],R[:int(dur*SR)],dur,.2,11000)
    if style=='funk':
        bpm=104*BPMX; beat=60/bpm; bar=4*beat
        bl=[(0,40,.5),(0.75,40,.25),(1.5,52,.25),(2,43,.5),(2.75,45,.25),(3.25,40,.25),(3.5,47,.4)]  # E minor-ish
        chord=[64,67,71,74]
        for b in range(int(dur/bar)+2):
            t0=b*bar; tr=0 if b%2==0 else 5
            for pos,nn,d in bl: st(L,R,t0+pos*beat,bass(midi(nn+tr),d*beat*1.6,0.5),.35)
            for k in (0.5,1.5,2.5,3.25):
                for j,nn in enumerate(chord): st(L,R,t0+k*beat+j*0.006,bp(ks(midi(nn+tr),beat*0.35,0.98,2),300,3500),.7,pan=.35)
            st(L,R,t0+3.5*beat,pad([midi(x+tr) for x in chord[:3]],beat*0.5),.25,-.3)
            for k in (0,1.75,2.5): st(L,R,t0+k*beat,kick(.8))
            for k in (1,3): st(L,R,t0+k*beat,snare(.45,200))
            for k in range(16): st(L,R,t0+k*beat/4,hat(.09 if k%2 else .13,.03),pan=-.3)
        return finish(L[:int(dur*SR)],R[:int(dur*SR)],dur,.12,10000)
    if style=='folk':
        bpm=92*BPMX; prog=[[55,59,62,67,71,79],[50,57,62,66,69,74],[52,59,64,67,71,76],[48,55,60,64,67,72]] # G D Em C open voicings
        beat=60/bpm; bar=4*beat
        pat=[(0,1,.9),(1,1,.6),(1.5,-1,.4),(2.5,-1,.45),(3,1,.6),(3.5,-1,.4)]
        mel=[74,76,79,76, 74,72,74,0, 71,74,76,79, 76,74,72,71]
        for b in range(int(dur/bar)+2):
            ch=prog[b%4]; t0=b*bar
            for pos,dirn,g in pat:
                notes=ch if dirn>0 else ch[::-1]
                for k,nn in enumerate(notes): st(L,R,t0+pos*beat+k*0.009,ks(midi(nn),beat*1.6,0.997,5),.16*g,pan=(-.3 if dirn>0 else .3))
            for k in range(4):
                m=mel[(b%4)*4+k]
                if m: st(L,R,t0+k*beat,rhodes(midi(m),beat*1.3)*0.0+pluck(midi(m),beat*1.3,2),.2)
            for k in (0,2): st(L,R,t0+k*beat,kick(.5))
            for k in (1,3): st(L,R,t0+k*beat,snare(.15,160))
            st(L,R,t0,bass(midi(ch[0]-12),beat*1.8),.35)
        return finish(L[:int(dur*SR)],R[:int(dur*SR)],dur,.22,9500)
style,dur,out=A.style,A.dur,A.out
x=song(style,dur); w=wave.open(out,'wb'); w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
w.writeframes((np.clip(x,-1,1)*32767).astype('<i2').tobytes()); w.close()
