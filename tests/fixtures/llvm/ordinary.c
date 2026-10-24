#include <stdio.h>
__attribute__((noinline)) int workloadChild(int value){return value+1;}
__attribute__((noinline)) int workload(void){return workloadChild(41);}
int main(void){printf("%d\n",workload());return 0;}
