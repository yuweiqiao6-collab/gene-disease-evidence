"""Prevent two web processes from mutating one research directory."""
import os
from pathlib import Path


class InstanceLock:
    def __init__(self,path:Path):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.handle=open(path,'a+b')
        os.chmod(path,0o600)
        try:
            if os.name=='nt':
                import msvcrt
                self.handle.seek(0)
                self.handle.write(b'0')
                self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise RuntimeError('此研究目录已有程序在运行；请关闭重复实例或指定不同运行目录。') from exc

    def close(self):
        if not self.handle.closed:
            if os.name=='nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            self.handle.close()
