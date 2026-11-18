"""Manual Linux integration proof with three disposable headless Twilights.

Run: VENV/bin/python tests/three_browser.py runtime
Only transport, port and process boundaries adapt the three local browsers;
the coordinator and participant export/diff/apply/baseline code is unchanged.
Set BFS_LAB_ROOT to a private directory on disk when /tmp is a small tmpfs.
No real profile is opened. Generated profiles contain only .invalid URLs.
"""
import asyncio,json,os,pathlib,socket,subprocess,sys,tempfile,time
R=pathlib.Path(sys.argv[1]).resolve(); sys.path.insert(0,str(R))
import structure_records as sr,structure,coordinator as c
from marionette_driver.marionette import Marionette
PYTHON=sys.executable
root=pathlib.Path(tempfile.mkdtemp(prefix='browser-focus-three-',dir=os.environ.get('BFS_LAB_ROOT')))
processes=[];ports=[];profiles=[]
def js(port,body,*args):
    client=Marionette(host='127.0.0.1',port=port,socket_timeout=40)
    try:
        client.start_session();client.set_context('chrome');client.timeout.script=30
        return client.execute_async_script('const done=arguments[arguments.length-1];const win=Services.wm.getMostRecentBrowserWindow();(async()=>{await win.gZenWorkspaces.promiseInitialized;'+body+'})().then(done,e=>done({error:String(e),stack:e.stack}));',script_args=list(args))
    finally:client.delete_session()
def state(port):
    result=sr.run_script(sr.STATE,[],port,30); assert result.get('ok'),result
    return result
def export(port,ids):
    result=sr.run_script(sr.EXPORT,[ids],port,30);assert result.get('ok'),result
    return result['records']
def apply(port,delivery):
    payload={'tabRecords':delivery.get('openedTabRecords',[]),'records':delivery.get('structureRecords',[]),'deletedIds':delivery.get('deletedStructureIds',[]),'closingTabIds':delivery.get('closedTabIds',[])}
    result=sr.run_script(sr.APPLY,[payload],port,30);assert result.get('ok'),result
    return result
def ids(s):return {key for key,value in s['records'].items() if value[0]=='tab'}
try:
    for i in range(3):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        profile=root/('profile-'+str(i));profile.mkdir();profiles.append(profile);ports.append(port)
        (profile/'user.js').write_text(f'user_pref("marionette.port",{port});\nuser_pref("remote.prefs.recommended",false);\nuser_pref("services.sync.engine.spaces",false);\nuser_pref("zen.spaces-sync.normal-tabs",true);\nuser_pref("zen.welcome-screen.seen",true);\nuser_pref("browser.shell.checkDefaultBrowser",false);\n')
        log=(root/('browser-'+str(i)+'.log')).open('w')
        proc=subprocess.Popen(['/opt/zen-twilight-bin/zen-bin','--headless','--no-remote','--profile',str(profile),'--marionette','--remote-allow-system-access'],stdout=log,stderr=log,env={**os.environ,'HOME':str(root),'MOZ_CRASHREPORTER_DISABLE':'1','MOZ_REMOTE_ALLOW_SYSTEM_ACCESS':'1'})
        processes.append(proc);end=time.monotonic()+30
        while True:
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2):break
            except OSError:
                if proc.poll() is not None or time.monotonic()>end:raise RuntimeError('Headless start failed '+str(root))
                time.sleep(.2)
    seed=js(ports[0],"const space=win.gZenWorkspaces.activeWorkspace;const tab=win.gBrowser.addTrustedTab('https://seed.invalid/',{inBackground:true,skipAnimation:true,createLazyBrowser:true,lazyTabTitle:'seed'});tab.id='lab-seed';tab.setAttribute('zen-workspace-id',space);win.gZenWorkspaces.moveTabToWorkspace(tab,space);return {space,tab:tab.id};")
    assert 'error' not in seed,seed
    a=state(ports[0]); records=export(ports[0],list(a['records']))
    delivery={'openedTabRecords':[r for r in records if r['cleartext']['kind']=='tab'],'structureRecords':[dict(r,op='create') for r in records if r['cleartext']['kind']!='tab']}
    spaceids=[key for key,val in a['records'].items() if val[0]=='space']
    for port in ports[1:]:
        apply(port,delivery)
        result=js(port,'for(const ws of win.gZenWorkspaces.getWorkspaces())if(!arguments[0].includes(ws.uuid))await win.gZenWorkspaces.removeWorkspace(ws.uuid);return true;',spaceids);assert result is True,result
    snapshots=[state(p) for p in ports]
    assert all(x['records']==snapshots[0]['records'] for x in snapshots),{'alignment_diffs':[{k for k in x['records'] if x['records'].get(k)!=snapshots[0]['records'].get(k)}.__repr__() for x in snapshots]}
    print('Three disposable Twilights aligned',flush=True)
    c.BASE=root/'hub';c.BASE.mkdir();c.STATE_DIR=root/'hub-state';c.STATE_DIR.mkdir();c.STATE_FILE=c.STATE_DIR/'state.json';c.LINUX_ACTIVE_BASELINE=c.BASE/'linux-active-baseline.json'
    c.linux_control.profile_path=lambda:profiles[0]
    c.linux_control.twilight_identity=lambda:str(processes[0].pid)
    c.linux_control.acquire=lambda:True;c.linux_control.release=lambda:True
    class Hub(c.ParticipantCoordinator):
        async def run_tool(self,*args):
            proc=await asyncio.create_subprocess_exec(PYTHON,*args,'--port',str(ports[0]),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await proc.communicate()
            try:result=json.loads(out)
            except ValueError:result=None
            return proc.returncode,result,out.decode()+err.decode()
    hub=Hub()
    async def run():
        await hub.save_linux_active_baseline(ids(snapshots[0]),snapshots[0]['records'],None)
        peers={name:{'port':ports[i],'state':snapshots[i]} for name,i in [('polaris',1),('workstation',2)]}
        for name,peer in peers.items():
            result=await hub.participant_event({'protocol':2,'participantId':name,'event':'participant-enroll','tabIds':sorted(ids(peer['state'])),'records':peer['state']['records']});assert result.get('ok'),result
        async def cycle(name):
            peer=peers[name];before=peer['state'];now=state(peer['port']);oldids=ids(before);newids=ids(now);opened=newids-oldids;closed=oldids-newids
            changed,deleted=structure.plan(before['records'],now['records'],set(now['presentIds']),same_browser=True,skip_ids=opened)
            tabrecords=export(peer['port'],sorted(opened));side=structure.outgoing(export(peer['port'],changed),before['records'])
            request=str(time.time_ns());result=await hub.participant_event({'protocol':2,'participantId':name,'event':'participant-exchange','requestId':request,'sameBrowser':True,'baselineRecords':before['records'],'closedTabIds':sorted(closed),'openedTabRecords':tabrecords,'structureRecords':side,'deletedStructureIds':deleted});assert result.get('ok'),result
            for batch in result['deliveries']:apply(peer['port'],batch)
            peer['state']=state(peer['port'])
            ack=await hub.participant_event({'protocol':2,'participantId':name,'event':'participant-ack','requestId':request,'tabIds':sorted(ids(peer['state'])),'baselineRecords':peer['state']['records']});assert ack.get('ok'),ack
        def add(port,name):
            result=js(port,"const tab=win.gBrowser.addTrustedTab('https://'+arguments[0]+'.invalid/',{inBackground:true,skipAnimation:true,createLazyBrowser:true,lazyTabTitle:arguments[0]});tab.id=arguments[0];tab.setAttribute('zen-workspace-id',arguments[1]);win.gZenWorkspaces.moveTabToWorkspace(tab,arguments[1]);return tab.id;",name,seed['space']);assert result==name,result
        for source,port in [('sirius',ports[0]),('polaris',ports[1]),('workstation',ports[2])]:
            name='lab-from-'+source;add(port,name);state(port)
            for _ in range(2):
                await cycle('polaris');await cycle('workstation')
            actual=[ids(state(p)) for p in ports]
            assert all(name in inventory for inventory in actual),(source,actual)
            print('PASS real addition '+source+' -> other two',flush=True)
        result=js(ports[2],"win.gBrowser.removeTab(win.document.getElementById(arguments[0]),{animate:false,skipPermitUnload:true});return true;",'lab-from-workstation');assert result is True,result
        state(ports[2])
        for _ in range(2):await cycle('workstation');await cycle('polaris')
        assert all('lab-from-workstation' not in ids(state(p)) for p in ports)
        print('PASS real closure workstation -> other two',flush=True)
    asyncio.run(run())
    # Exercise participant_cycle itself, preserving its original export, diff,
    # apply and baseline functions. Only transport/port/process boundaries adapt
    # the three local headless processes to their separate host roles.
    import mac_agent as m
    original_run=m.run
    agent_data={name:root/(name+'-agent') for name in ('polaris','workstation')}
    for name,data in agent_data.items():
        data.mkdir()
        index=1 if name=='polaris' else 2
        initial=state(ports[index])
        (data/'active-baseline.json').write_text(json.dumps({'identity':str(processes[index].pid),'tabIds':sorted(ids(initial)),'records':initial['records']}))
    def agent_cycle(name,offline=False):
        index=1 if name=='polaris' else 2
        port=ports[index]
        m.BASE=agent_data[name];m.ACTIVE_BASELINE=m.BASE/'active-baseline.json'
        m.cfg.PARTICIPANT_ID=name
        m.ensure_control=lambda:True;m.release_control=lambda:True
        m.twilight_identity=lambda:str(processes[index].pid)
        m.twilight_profile=lambda:profiles[index]
        m.live_tab_ids=lambda:ids(state(port))
        def run_at_port(command,**kwargs):
            if len(command)>1 and pathlib.Path(command[1]).name in ('structure_records.py','export_tab_records.py','sync_now.py'):
                command=[*command,'--port',str(port)]
            return original_run(command,**kwargs)
        m.run=run_at_port
        def rpc(message):
            if offline:return None
            return asyncio.run(hub.participant_event(message))
        m.participant_rpc=rpc
        return m.participant_cycle()
    for name,index in [('polaris',1),('workstation',2)]:
        marker='real-cycle-'+name
        result=js(ports[index],"const tab=win.gBrowser.addTrustedTab('https://'+arguments[0]+'.invalid/',{inBackground:true,skipAnimation:true,createLazyBrowser:true,lazyTabTitle:arguments[0]});tab.id=arguments[0];tab.setAttribute('zen-workspace-id',arguments[1]);win.gZenWorkspaces.moveTabToWorkspace(tab,arguments[1]);return tab.id;",marker,seed['space']);assert result==marker,result
        # The production change detector observes Zen's asynchronously saved file.
        # Flush that native snapshot explicitly before testing the transport.
        state(ports[index])
        for _ in range(2):
            assert agent_cycle('polaris')
            assert agent_cycle('workstation')
        assert all(marker in ids(state(p)) for p in ports)
        print('PASS original participant_cycle '+name+' -> other two',flush=True)
    # A failed RPC leaves an exact request; an additional local tab must survive
    # its retry while the third host has added a concurrent tab.
    assert not agent_cycle('workstation',offline=True)
    for index,marker in [(1,'offline-other'),(2,'offline-later-local')]:
        result=js(ports[index],"const tab=win.gBrowser.addTrustedTab('https://'+arguments[0]+'.invalid/',{inBackground:true,skipAnimation:true,createLazyBrowser:true,lazyTabTitle:arguments[0]});tab.id=arguments[0];tab.setAttribute('zen-workspace-id',arguments[1]);win.gZenWorkspaces.moveTabToWorkspace(tab,arguments[1]);return tab.id;",marker,seed['space']);assert result==marker,result
        state(ports[index])
    for _ in range(3):
        assert agent_cycle('polaris')
        assert agent_cycle('workstation')
    inventories=[ids(state(p)) for p in ports]
    assert all({'offline-other','offline-later-local'}<=inventory for inventory in inventories),inventories
    print('PASS original agent offline retry preserves concurrent additions on all three',flush=True)
    print('Lab artifact '+str(root),flush=True)
finally:
    for proc in processes:
        if proc.poll() is None:proc.terminate()
    for proc in processes:
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
