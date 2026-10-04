// SPDX-License-Identifier: GPL-3.0-or-later
// Muxiveo : MVTools en flux Y4M, sans interpréteur Python.
#include "y4m.h"
#include <VapourSynth4.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <filesystem>
#include <list>
#include <map>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <thread>
#include <limits>
#ifdef _WIN32
#include <windows.h>
#include <io.h>
#include <fcntl.h>
#elif defined(__APPLE__)
#include <mach-o/dyld.h>
#include <dlfcn.h>
#include <signal.h>
#else
#include <unistd.h>
#include <dlfcn.h>
#include <signal.h>
#endif

using Bytes = std::vector<uint8_t>;
using Frame = std::shared_ptr<Bytes>;
namespace fs = std::filesystem;
struct Failure : std::runtime_error { int code; Failure(int c, const std::string& m):std::runtime_error(m),code(c){} };
struct Options {
    std::string input="-", output="-", mode="standard", fps, matrix="bt709", range="limited", chroma="left";
    int factor=2, threads=0, window=8;
    double threshold=10.0, interval=2.0;
    bool selftest=false, json=false, version=false;
};
static int64_t multiply(int64_t a, int64_t b) {
    if (a<0 || b<=0 || a>std::numeric_limits<int64_t>::max()/b) throw Failure(1,"cadence ou durée trop grande");
    return a*b;
}
static int64_t ceiling(int64_t a, int64_t b) { return a/b+(a%b!=0); }
static int64_t integer(const std::string& s) {
    size_t n=0; long long v;
    try { v=std::stoll(s,&n); } catch(...) { throw Failure(1,"entier invalide : "+s); }
    if(n!=s.size() || v<0 || v>1000000000) throw Failure(1,"entier hors limites : "+s);
    return v;
}
static double decimal(const std::string& s) {
    size_t n=0; double v;
    try { v=std::stod(s,&n); } catch(...) { throw Failure(1,"nombre invalide : "+s); }
    if(n!=s.size() || !std::isfinite(v)) throw Failure(1,"nombre invalide : "+s);
    return v;
}
static Options parse(int argc,char** argv) {
    Options o;bool factor=false;
    for(int i=1;i<argc;i++) {
        std::string a=argv[i];
        auto value=[&](){ if(++i>=argc)throw Failure(1,"valeur manquante : "+a);return std::string(argv[i]); };
        if(a=="--version")o.version=true;
        else if(a=="--self-test")o.selftest=true;
        else if(a=="--json")o.json=true;
        else if(a=="-i")o.input=value();
        else if(a=="-o")o.output=value();
        else if(a=="--factor"){o.factor=(int)integer(value());factor=true;}
        else if(a=="--fps")o.fps=value();
        else if(a=="--mode")o.mode=value();
        else if(a=="--threads")o.threads=(int)integer(value());
        else if(a=="--window-frames")o.window=(int)integer(value()); // diagnostic des frontières
        else if(a=="--scene-threshold")o.threshold=decimal(value());
        else if(a=="--progress-interval")o.interval=decimal(value());
        else if(a=="--matrix")o.matrix=value();
        else if(a=="--range")o.range=value();
        else if(a=="--chroma-loc")o.chroma=value();
        else if(a=="--help" || a=="-h") {
            std::puts("muxiveo-mvtools : Y4M stdin/stdout ou -i/-o fichier\n"
                      "--factor 2|3|4 OU --fps num/den ; --mode standard|uhd\n"
                      "--threads N (0 automatique) ; --scene-threshold 0..100\n"
                      "--progress-interval secondes ; --version ; --self-test --json");
            std::exit(0);
        } else throw Failure(1,"option inconnue : "+a);
    }
    if(factor && !o.fps.empty())throw Failure(1,"--factor et --fps sont exclusifs");
    if(o.mode!="standard" && o.mode!="uhd")throw Failure(1,"mode invalide");
    if(o.factor<2 || o.factor>4 || o.threads>256 || o.window<1 || o.window>16 || o.threshold<0 || o.threshold>100 || o.interval<0.01)
        throw Failure(1,"paramètre hors limites");
    if(o.range!="limited" && o.range!="full")throw Failure(1,"plage couleur invalide");
    if(o.chroma!="left" && o.chroma!="center" && o.chroma!="topleft")throw Failure(1,"position chroma invalide");
    return o;
}
static fs::path executable_path() {
#ifdef _WIN32
    std::vector<wchar_t> p(32768);auto n=GetModuleFileNameW(nullptr,p.data(),(DWORD)p.size());
    if(!n || n>=p.size())throw Failure(3,"chemin exécutable illisible");return fs::path(std::wstring(p.data(),n));
#elif defined(__APPLE__)
    uint32_t n=0;_NSGetExecutablePath(nullptr,&n);std::vector<char> p(n);
    if(_NSGetExecutablePath(p.data(),&n))throw Failure(3,"chemin exécutable illisible");return fs::canonical(p.data());
#else
    return fs::canonical("/proc/self/exe");
#endif
}
struct Library {
#ifdef _WIN32
    HMODULE handle=nullptr;
    explicit Library(const fs::path& p) {handle=LoadLibraryExW(p.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);if(!handle)throw Failure(3,"bibliothèque illisible : "+p.u8string());}
    void* symbol(const char* s){return (void*)GetProcAddress(handle,s);}
    ~Library(){if(handle)FreeLibrary(handle);}
#else
    void* handle=nullptr;
    explicit Library(const fs::path& p) {handle=dlopen(p.c_str(),RTLD_NOW|RTLD_LOCAL);if(!handle)throw Failure(3,std::string("bibliothèque illisible : ")+dlerror());}
    void* symbol(const char* s){return dlsym(handle,s);}
    ~Library(){if(handle)dlclose(handle);}
#endif
};
static const char* extension() {
#ifdef _WIN32
    return ".dll";
#elif defined(__APPLE__)
    return ".dylib";
#else
    return ".so";
#endif
}
struct Runtime {
    fs::path dir;
    std::unique_ptr<Library> library;
    const VSAPI* api=nullptr;VSCore* core=nullptr;VSPlugin* mv=nullptr;
    explicit Runtime(int threads):dir(executable_path().parent_path()/"mvtools-runtime") {
        for(auto name:{"libvapoursynth","libvapoursynthfilters","mvtools"})
            if(!fs::is_regular_file(dir/(std::string(name)+extension())))throw Failure(3,"runtime incomplet : "+std::string(name));
        library=std::make_unique<Library>(dir/(std::string("libvapoursynth")+extension()));
        auto get=(const VSAPI* (VS_CC *)(int))library->symbol("getVapourSynthAPI");
        if(!get || !(api=get(VAPOURSYNTH_API_VERSION)))throw Failure(3,"API VapourSynth incompatible");
        core=api->createCore(ccfDisableAutoLoading);
        if(!core)throw Failure(3,"initialisation VapourSynth impossible");
        api->setThreadCount(threads,core);
        VSCoreInfo info;api->getCoreInfo(core,&info);
        if(info.core!=80)throw Failure(3,"VapourSynth R80 requis");
        VSPlugin* std=api->getPluginByID("com.vapoursynth.std",core);
        VSMap* args=api->createMap();std::string path=(dir/(std::string("mvtools")+extension())).u8string();
        api->mapSetData(args,"path",path.c_str(),(int)path.size(),dtUtf8,maReplace);
        VSMap* result=api->invoke(std,"LoadPlugin",args);api->freeMap(args);
        const char* error=api->mapGetError(result);std::string message=error?error:"";api->freeMap(result);
        if(!message.empty())throw Failure(3,message);
        mv=api->getPluginByNamespace("mv",core);
        if(!mv)throw Failure(3,"plugin MVTools absent");
        if(api->getPluginVersion(mv)!=VS_MAKE_VERSION(29,0))throw Failure(3,"MVTools v29 requis");
    }
    ~Runtime(){if(core)api->freeCore(core);}
};
struct Window {FrameFormat fmt;Options options;std::vector<Frame> frames;};
static const VSFrame* VS_CC source_frame(int n,int reason,void* data,void**,VSFrameContext*,VSCore* core,const VSAPI* api) {
    if(reason!=arInitial)return nullptr;
    auto& w=**(std::shared_ptr<Window>*)data;
    VSVideoFormat format;api->queryVideoFormat(&format,cfYUV,stInteger,w.fmt.bit_depth,w.fmt.sub_x==2,w.fmt.sub_y==2,core);
    VSFrame* frame=api->newVideoFrame(&format,w.fmt.width,w.fmt.height,nullptr,core);
    const uint8_t* input=w.frames.at(n)->data();
    for(int p=0;p<3;p++) {
        int width=api->getFrameWidth(frame,p),height=api->getFrameHeight(frame,p);
        auto stride=api->getStride(frame,p);uint8_t* output=api->getWritePtr(frame,p);
        size_t row=(size_t)width*w.fmt.bytes_per_sample;
        for(int y=0;y<height;y++){std::memcpy(output+y*stride,input,row);input+=row;}
    }
    auto props=api->getFramePropertiesRW(frame);
    api->mapSetInt(props,"_FieldBased",0,maReplace);
    api->mapSetInt(props,"_ColorRange",w.options.range=="full"?0:1,maReplace);
    api->mapSetInt(props,"_ChromaLocation",w.options.chroma=="center"?1:w.options.chroma=="topleft"?2:0,maReplace);
    api->mapSetInt(props,"_Matrix",w.options.matrix=="bt2020nc"||w.options.matrix=="bt2020"?9:w.options.matrix=="bt709"?1:6,maReplace);
    return frame;
}
static void VS_CC source_free(void* data,VSCore*,const VSAPI*) {delete (std::shared_ptr<Window>*)data;}
struct Graph {
    Runtime& r;std::vector<VSNode*> nodes;VSNode* source=nullptr;VSNode* super=nullptr;VSNode* backward=nullptr;VSNode* forward=nullptr;
    std::map<int,VSNode*> phases;std::list<int> recent;
    VSNode* invoke(const char* name,std::initializer_list<std::pair<const char*,VSNode*>> clips,
                   std::initializer_list<std::pair<const char*,int64_t>> ints={},
                   std::initializer_list<std::pair<const char*,double>> floats={}) {
        VSMap* args=r.api->createMap();
        for(auto x:clips)r.api->mapSetNode(args,x.first,x.second,maReplace);
        for(auto x:ints)r.api->mapSetInt(args,x.first,x.second,maReplace);
        for(auto x:floats)r.api->mapSetFloat(args,x.first,x.second,maReplace);
        VSMap* out=r.api->invoke(r.mv,name,args);r.api->freeMap(args);
        auto error=r.api->mapGetError(out);std::string message=error?error:"";
        VSNode* node=nullptr;int err=0;if(message.empty())node=r.api->mapGetNode(out,"clip",0,&err);
        r.api->freeMap(out);if(!node)throw Failure(3,std::string(name)+": "+message);
        nodes.push_back(node);return node;
    }
    Graph(Runtime& runtime,std::shared_ptr<Window> window):r(runtime) {
        try {
        const bool uhd=window->options.mode=="uhd";
        r.api->setMaxCacheSize((uhd?1024LL:512LL)*1024*1024,r.core);
        VSVideoInfo vi{};auto& f=window->fmt;
        r.api->queryVideoFormat(&vi.format,cfYUV,stInteger,f.bit_depth,f.sub_x==2,f.sub_y==2,r.core);
        vi.width=f.width;vi.height=f.height;vi.fpsNum=f.fps_num;vi.fpsDen=f.fps_den;vi.numFrames=(int)window->frames.size();
        source=r.api->createVideoFilter2("MuxiveoY4M",&vi,source_frame,source_free,fmParallel,nullptr,0,new std::shared_ptr<Window>(window),r.core);
        if(!source)throw Failure(3,"source Y4M impossible");nodes.push_back(source);
        super=invoke("Super",{{"clip",source}},{{"pel",uhd?4:2},{"hpad",32},{"vpad",32},{"chroma",1},{"sharp",2},{"rfilter",2}});
        for(int isb=0;isb<2;isb++) {
            auto vector=invoke("Analyse",{{"super",super}},{{"isb",isb},{"delta",1},{"blksize",uhd?32:16},{"overlap",uhd?16:8},
                {"search",uhd?3:4},{"searchparam",uhd?8:2},{"search_coarse",3},{"chroma",1},{"truemotion",1},{"global",1},{"trymany",uhd},{"dct",uhd?5:0}});
            if(uhd)for(int block:{16,8})vector=invoke("Recalculate",{{"super",super},{"vectors",vector}},
                {{"blksize",block},{"overlap",block/2},{"search",3},{"searchparam",block==16?4:2},{"dct",5},{"thsad",200},{"chroma",1},{"truemotion",1}});
            (isb?backward:forward)=vector;
        }
        } catch(...) { for(auto i=nodes.rbegin();i!=nodes.rend();++i)r.api->freeNode(*i);throw; }
    }
    ~Graph(){for(auto x:phases)r.api->freeNode(x.second);for(auto i=nodes.rbegin();i!=nodes.rend();++i)r.api->freeNode(*i);}
    Bytes interpolate(int index,int phase) {
        auto found=phases.find(phase);VSNode* node;
        if(found==phases.end()) {
            // Phase amont à 1/256 ; marge interne pour éviter un arrondi float vers le bas.
            node=invoke("FlowInter",{{"clip",source},{"super",super},{"mvbw",backward},{"mvfw",forward}},
                {{"blend",0},{"thscd1",400},{"thscd2",130}},{{"time",(phase+0.125)*100.0/256.0},{"ml",100.0}});
            nodes.pop_back();phases.emplace(phase,node);
            if(phases.size()>8){int old=recent.back();recent.pop_back();r.api->freeNode(phases.at(old));phases.erase(old);}
        } else node=found->second;
        recent.remove(phase);recent.push_front(phase);
        char error[4096]={};auto frame=r.api->getFrame(index,node,error,sizeof(error));
        if(!frame)throw Failure(3,std::string("rendu MVTools : ")+error);
        auto vi=r.api->getVideoInfo(source);size_t size=(size_t)vi->width*vi->height*vi->format.bytesPerSample;
        for(int p=1;p<3;p++)size+=(size_t)r.api->getFrameWidth(frame,p)*r.api->getFrameHeight(frame,p)*vi->format.bytesPerSample;
        Bytes out(size);auto dst=out.data();
        for(int p=0;p<3;p++) {
            size_t row=(size_t)r.api->getFrameWidth(frame,p)*vi->format.bytesPerSample;int h=r.api->getFrameHeight(frame,p);
            auto src=r.api->getReadPtr(frame,p);auto stride=r.api->getStride(frame,p);
            for(int y=0;y<h;y++){std::memcpy(dst,src+y*stride,row);dst+=row;}
        }
        r.api->freeFrame(frame);return out;
    }
};
static double mafd(const Bytes& a,const Bytes& b,const FrameFormat& f) {
    uint64_t sum=0,count=0;int step=std::max(1,std::min(f.width,f.height)/540);
    for(int y=0;y<f.height;y+=step)for(int x=0;x<f.width;x+=step) {
        size_t i=(size_t)y*f.width+x;
        int av=f.bytes_per_sample==1?a[i]:a[2*i]|a[2*i+1]<<8;
        int bv=f.bytes_per_sample==1?b[i]:b[2*i]|b[2*i+1]<<8;
        sum+=std::abs(av-bv);count++;
    }
    return (double)sum*100.0/count/(1u<<f.bit_depth);
}
static void self_test(Runtime& runtime,const Options& opts) {
    uint64_t hash=1469598103934665603ULL;
    for(int bits:{8,10})for(auto mode:{"standard","uhd"}) {
        auto window=std::make_shared<Window>();window->options=opts;window->options.mode=mode;
        auto& f=window->fmt;f.width=96;f.height=64;f.fps_num=24;f.fps_den=1;f.bit_depth=bits;f.bytes_per_sample=bits>8?2:1;
        for(int i=0;i<6;i++) {
            auto data=std::make_shared<Bytes>(f.frame_bytes());
            for(size_t j=0;j<(size_t)f.total_samples();j++){unsigned v=(unsigned)((j+std::clamp(i-2,0,1)*3)%256)<<(bits-8);(*data)[j*f.bytes_per_sample]=v&255;if(bits>8)(*data)[j*2+1]=v>>8;}
            window->frames.push_back(data);
        }
        Graph graph(runtime,window);auto data=graph.interpolate(2,128);
        for(auto b:data){hash^=b;hash*=1099511628211ULL;}
    }
    std::printf("{\"ok\":true,\"version\":\"%s\",\"vapoursynth\":80,\"mvtools\":29,\"mvtools_revision\":\"v29_2\",\"zimg\":\"3.0.6\",\"fftw\":\"3.3.11\",\"bits\":[8,10],\"modes\":[\"standard\",\"uhd\"],\"render_hash\":\"%llx\"}\n",MUXIVEO_MVTOOLS_VERSION,(unsigned long long)hash);
}
static int process(const Options& options) {
    FILE* input=stdin;FILE* output=stdout;
    using File=std::unique_ptr<FILE,int(*)(FILE*)>;File infile(nullptr,std::fclose),outfile(nullptr,std::fclose);
    auto open_file=[](const std::string& path,bool write) {
#ifdef _WIN32
        return _wfopen(fs::u8path(path).c_str(),write?L"wb":L"rb");
#else
        return std::fopen(path.c_str(),write?"wb":"rb");
#endif
    };
    if(options.input!="-"){infile.reset(open_file(options.input,false));if(!infile)throw Failure(4,"entrée illisible");input=infile.get();}
    if(options.output!="-"){outfile.reset(open_file(options.output,true));if(!outfile)throw Failure(4,"sortie illisible");output=outfile.get();}
    Y4mReader reader(input);Y4mWriter writer(output);std::string error;
    if(!reader.read_header(error))throw Failure(2,error);
    auto f=reader.format();
    if(f.width<64||f.height<64||f.width>16384||f.height>16384||f.width%f.sub_x||f.height%f.sub_y)throw Failure(2,"dimensions incompatibles (64..16384, multiples du sous-échantillonnage)");
    if(f.interlace!='p' && f.interlace!='?')throw Failure(2,"désentrelacer avant MVTools");
    int64_t p=options.factor,q=1;
    if(!options.fps.empty()) {
        auto slash=options.fps.find('/');auto num=integer(options.fps.substr(0,slash));auto den=slash==std::string::npos?1:integer(options.fps.substr(slash+1));
        if(!num||!den)throw Failure(1,"cadence invalide");p=multiply(num,f.fps_den);q=multiply(den,f.fps_num);
    }
    auto gcd=std::gcd(p,q);p/=gcd;q/=gcd;if(p<=q||p>multiply(q,1000))throw Failure(1,"cadence cible hors limites");
    auto out_format=f;out_format.fps_num=multiply(f.fps_num,p);out_format.fps_den=multiply(f.fps_den,q);
    gcd=std::gcd(out_format.fps_num,out_format.fps_den);out_format.fps_num/=gcd;out_format.fps_den/=gcd;
    int budget=(int)std::max(1u,std::thread::hardware_concurrency());int threads=options.threads?options.threads:options.mode=="standard"?std::max(1,std::min(4,budget/2)):budget;
    Runtime runtime(threads);
    if(!writer.write_header(out_format,reader.passthrough_tokens()))throw Failure(4,"écriture en-tête impossible");
    std::fprintf(stderr,"info: MVTools %s | %dx%d %d bits | CPU threads=%d | pel=%d | phase=1/256\n",options.mode.c_str(),f.width,f.height,f.bit_depth,threads,options.mode=="uhd"?4:2);
    std::deque<Frame> buffer;int64_t first=0,total=0,out=0,done=0,cuts=0,statics=0;bool eof=false;double previous=0;
    auto start=std::chrono::steady_clock::now(),report=start;
    auto progress=[&](){double secs=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();std::fprintf(stderr,"progress in=%lld out=%lld scenes=%lld static=%lld fps=%.2f\n",(long long)done,(long long)out,(long long)cuts,(long long)statics,out/std::max(0.001,secs));report=std::chrono::steady_clock::now();};
    auto read_to=[&](int64_t n){while(!eof&&total<n){auto data=std::make_shared<Bytes>(f.frame_bytes());int status=reader.read_frame(data->data(),error);if(status<0)throw Failure(2,error);if(!status)eof=true;else{buffer.push_back(data);total++;}}};
    for(int64_t base=0;;base+=options.window) {
        read_to(base+options.window+2);if(base>=total)break;
        int useful=(int)std::min<int64_t>(options.window,total-base);
        auto window=std::make_shared<Window>();window->fmt=f;window->options=options;
        for(int i=-2;i<useful+2;i++){int64_t global=std::clamp<int64_t>(base+i,0,total-1);window->frames.push_back(buffer.at((size_t)(global-first)));}
        std::unique_ptr<Graph> graph;
        for(int n=0;n<useful;n++) {
            auto a=window->frames[n+2],b=window->frames[n+3];double diff=mafd(*a,*b,f);double score=std::min(diff,std::fabs(diff-previous));previous=diff;
            bool is_static=*a==*b,cut=!is_static&&options.threshold>0&&score>=options.threshold;
            statics+=is_static;cuts+=cut;
            int64_t i=base+n,end=ceiling(multiply(i+1,p),q);
            while(out<end) {
                int64_t phase=multiply(out,q)-multiply(i,p);
                if(phase<0||phase>=p)throw Failure(3,"ordonnancement invalide");
                if(phase==0||is_static||cut||i+1>=total){if(!writer.write_frame(a->data(),a->size()))throw Failure(4,"pipe de sortie fermé");}
                else{if(!graph)graph=std::make_unique<Graph>(runtime,window);auto image=graph->interpolate(n+2,(int)(multiply(phase,256)/p));if(!writer.write_frame(image.data(),image.size()))throw Failure(4,"pipe de sortie fermé");}
                out++;
                if(std::chrono::duration<double>(std::chrono::steady_clock::now()-report).count()>=options.interval)progress();
            }
            done=i+1;
        }
        graph.reset();window.reset();int64_t keep=base+useful-2;
        while(first<keep&&!buffer.empty()){buffer.pop_front();first++;}
    }
    if(!total)throw Failure(2,"flux sans images");if(!writer.flush())throw Failure(4,"vidage de sortie impossible");progress();return 0;
}
static int run_main(int argc,char** argv) {
#ifndef _WIN32
    signal(SIGPIPE,SIG_IGN);
#else
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
#endif
    try {
        auto options=parse(argc,argv);
        if(options.version){std::printf("muxiveo-mvtools %s\n",MUXIVEO_MVTOOLS_VERSION);return 0;}
        if(options.selftest){Runtime runtime(options.threads?options.threads:2);self_test(runtime,options);return 0;}
        return process(options);
    }catch(const std::bad_alloc&){std::fprintf(stderr,"error: mémoire RAM insuffisante ; choisir Standard ou réduire les tâches parallèles\n");return 5;}
    catch(const Failure& e){std::fprintf(stderr,"error: %s\n",e.what());return e.code;}
    catch(const std::exception& e){std::fprintf(stderr,"error: %s\n",e.what());return 3;}
}
#ifdef _WIN32
int wmain(int argc,wchar_t** argv) {
    std::vector<std::string> strings;std::vector<char*> arguments;
    for(int i=0;i<argc;i++)strings.push_back(fs::path(argv[i]).u8string());
    for(auto& value:strings)arguments.push_back(value.data());
    return run_main(argc,arguments.data());
}
#else
int main(int argc,char** argv) { return run_main(argc,argv); }
#endif
