#include <stdio.h>
#include <stddef.h>
#include <libproc.h>
#include <sys/proc_info.h>
#define P(T) printf("sizeof(" #T ")=%zu\n", sizeof(T))
#define O(T,F) printf("offsetof(" #T "," #F ")=%zu\n", offsetof(T,F))
int main(void){
 P(struct proc_fdinfo); P(struct proc_fileinfo); P(struct socket_info); P(struct socket_fdinfo);
 P(struct in_sockinfo); P(struct tcp_sockinfo); P(struct proc_bsdinfo); P(struct vinfo_stat);
 O(struct socket_fdinfo,pfi.fi_status); O(struct socket_fdinfo,psi);
 O(struct socket_fdinfo,psi.soi_so); O(struct socket_fdinfo,psi.soi_pcb);
 O(struct socket_fdinfo,psi.soi_type); O(struct socket_fdinfo,psi.soi_protocol); O(struct socket_fdinfo,psi.soi_family);
 O(struct socket_fdinfo,psi.soi_state); O(struct socket_fdinfo,psi.soi_kind); O(struct socket_fdinfo,psi.soi_proto);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_ini.insi_fport);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_ini.insi_lport);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_ini.insi_gencnt);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_ini.insi_vflag);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_ini.insi_faddr.ina_46.i46a_addr4);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_ini.insi_laddr.ina_46.i46a_addr4);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_state);
 O(struct socket_fdinfo,psi.soi_proto.pri_tcp.tcpsi_tp);
 printf("PROC_PIDLISTFDS=%d PROC_PIDFDSOCKETINFO=%d PROX_FDTYPE_SOCKET=%d\n",PROC_PIDLISTFDS,PROC_PIDFDSOCKETINFO,PROX_FDTYPE_SOCKET);
 return 0;}
